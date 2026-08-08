import { useState } from 'react';
import { ConfirmDialog } from './ConfirmDialog';
import {
  recordFindingResolution,
  type RequalificationFinding,
  type RequalificationReadiness,
} from '../api';

// Severity badge styling — mirrors AuditFindingsList in VersionLifecyclePanelPrimitives.
const FINDING_SEVERITY_STYLE: Record<string, string> = {
  critical: 'bg-red-500/10 text-red-400 border-red-500/20',
  high: 'bg-orange-500/10 text-orange-400 border-orange-500/20',
  medium: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
  low: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
};
const FINDING_SEVERITY_ORDER = ['critical', 'high', 'medium', 'low'];

// Per-finding resolution-ledger state badges.
const RESOLUTION_STATE_STYLE: Record<string, string> = {
  resolved: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
  stale: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
  unresolved: 'bg-red-500/10 text-red-400 border-red-500/20',
};
const RESOLUTION_STATE_LABEL: Record<string, string> = {
  resolved: 'resolved',
  stale: 'stale',
  unresolved: 'unresolved',
};

interface ReleaseQualificationFindingsProps {
  /** Ledger-backed re-qualification readiness for the open release version. */
  readiness?: RequalificationReadiness | null;
  projectId?: string | null;
  /** Hides the dispose controls (read-only operator view). */
  readOnly?: boolean;
  /** Called after a successful resolution write so the parent refreshes. */
  onRefresh?: () => void;
}

interface DisposalTarget {
  finding: RequalificationFinding;
  state: 'resolved' | 'stale';
}

/**
 * Operator surface for the audit-finding resolution ledger. Renders the open
 * version's carried high/medium audit findings with their ledger state
 * (resolved | stale | unresolved) and lets a member dispose an unresolved
 * blocking finding in-product — instead of hand-crafting an API call with a
 * 16-hex key the UI never displayed.
 */
export function ReleaseQualificationFindings({
  readiness,
  projectId,
  readOnly = false,
  onRefresh,
}: ReleaseQualificationFindingsProps) {
  const [dialog, setDialog] = useState<DisposalTarget | null>(null);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  if (!readiness) return null;

  const {
    requalification_ready: ready,
    reason,
    gating_total: gatingTotal,
    gating_resolved: gatingResolved,
    audit_findings_truncated: truncated,
    findings,
    open_version_id: openVersionId,
  } = readiness;

  const findingsSorted = [...(findings ?? [])].sort((a, b) => {
    const ai = FINDING_SEVERITY_ORDER.indexOf(a.severity?.toLowerCase() ?? '');
    const bi = FINDING_SEVERITY_ORDER.indexOf(b.severity?.toLowerCase() ?? '');
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });

  const confirmDispose = async (target: DisposalTarget, note: string) => {
    if (!projectId || !openVersionId || actionLoading) return;
    setActionLoading(`${target.finding.key}:${target.state}`);
    setActionMsg(null);
    try {
      await recordFindingResolution(projectId, {
        finding_key: target.finding.key,
        release_version_id: openVersionId,
        state: target.state,
        severity: target.finding.severity,
        note,
      });
      setActionMsg(
        target.state === 'resolved'
          ? 'Marked finding resolved'
          : 'Marked finding stale',
      );
      onRefresh?.();
    } catch (err) {
      setActionMsg(
        err instanceof Error ? err.message : 'Failed to record finding resolution',
      );
    } finally {
      setActionLoading(null);
    }
  };

  return (
    <div
      data-testid="requalification-readiness"
      className="pl-2 border-l border-neutral-700/50 space-y-1"
    >
      <div className="flex flex-wrap items-center gap-1.5 text-ui-sm">
        <span className="text-neutral-500 uppercase tracking-wide">
          Re-qualification readiness
        </span>
        <span
          data-testid="requalification-ready-badge"
          className={`text-ui-sm font-semibold px-1.5 py-0.5 rounded border ${
            ready
              ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
              : 'bg-amber-500/10 text-amber-400 border-amber-500/30'
          }`}
        >
          {ready ? 'ready to re-qualify' : 'blocked by carried findings'}
        </span>
        {gatingTotal > 0 && (
          <span className="text-neutral-600">
            {gatingResolved}/{gatingTotal} gating finding
            {gatingTotal !== 1 ? 's' : ''} resolved
          </span>
        )}
      </div>
      {reason && (
        <p className="text-ui-sm text-neutral-400" data-testid="requalification-reason">
          {reason}
        </p>
      )}
      {truncated && (
        <p className="text-ui-xs text-amber-400">
          carried findings were truncated at the cap — re-qualification re-scores
          the full finding set
        </p>
      )}
      {findingsSorted.length > 0 ? (
        <div className="space-y-0.5">
          {findingsSorted.map((f) => {
            const unresolved = f.state === 'unresolved';
            const disposeKey = `${f.key}:${dialog?.state ?? ''}`;
            const loading = actionLoading === disposeKey;
            return (
              <div
                key={f.key}
                data-testid="requalification-finding"
                data-finding-state={f.state}
                className="flex flex-wrap items-center gap-1.5 text-ui-sm"
              >
                <span
                  className={`font-semibold shrink-0 px-1.5 py-0.5 rounded border ${
                    FINDING_SEVERITY_STYLE[f.severity?.toLowerCase() ?? ''] ??
                    'bg-neutral-800/50 text-neutral-400 border-neutral-700/50'
                  }`}
                >
                  {f.severity ?? '?'}
                </span>
                <span className="min-w-0 break-words text-neutral-300">
                  {f.summary ?? '(no summary)'}
                </span>
                <span
                  className={`shrink-0 px-1.5 py-0.5 rounded border ${
                    RESOLUTION_STATE_STYLE[f.state] ??
                    'bg-neutral-800 text-neutral-400 border-neutral-700'
                  }`}
                  title={`finding key: ${f.key}`}
                >
                  {RESOLUTION_STATE_LABEL[f.state] ?? f.state}
                </span>
                {unresolved && projectId && !readOnly && (
                  <span className="flex flex-wrap items-center gap-1">
                    <button
                      onClick={() => setDialog({ finding: f, state: 'resolved' })}
                      disabled={actionLoading !== null}
                      className="text-ui-sm px-2 py-0.5 min-h-[44px] min-w-[44px] sm:min-h-0 rounded border border-emerald-500/40 text-emerald-400 hover:bg-emerald-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                      title="Record that this finding is demonstrably fixed (e.g. a merged fix landed)"
                      data-testid="resolve-finding"
                    >
                      {loading && dialog?.state === 'resolved' ? '…' : 'Resolved'}
                    </button>
                    <button
                      onClick={() => setDialog({ finding: f, state: 'stale' })}
                      disabled={actionLoading !== null}
                      className="text-ui-sm px-2 py-0.5 min-h-[44px] min-w-[44px] sm:min-h-0 rounded border border-blue-500/40 text-blue-400 hover:bg-blue-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                      title="Record that this finding no longer applies to the current code"
                      data-testid="stale-finding"
                    >
                      {loading && dialog?.state === 'stale' ? '…' : 'Stale'}
                    </button>
                  </span>
                )}
              </div>
            );
          })}
        </div>
      ) : (
        <p className="text-ui-sm text-neutral-600 italic">no carried gating findings</p>
      )}
      {actionMsg && (
        <p data-testid="requalification-action-msg" className="text-ui-sm text-neutral-400 italic">
          {actionMsg}
        </p>
      )}
      {dialog && (
        <ConfirmDialog
          title={
            dialog.state === 'resolved' ? 'Mark finding resolved?' : 'Mark finding stale?'
          }
          message={
            dialog.state === 'resolved'
              ? `Record an operator disposition that "${dialog.finding.summary ?? dialog.finding.key}" is demonstrably fixed. This clears the finding from the re-qualification gate.`
              : `Record an operator disposition that "${dialog.finding.summary ?? dialog.finding.key}" no longer applies to the current code. This clears the finding from the re-qualification gate.`
          }
          confirmLabel={dialog.state === 'resolved' ? 'Mark resolved' : 'Mark stale'}
          attestationInput={{
            label: 'Reason / fix reference (required)',
            placeholder: 'e.g. fixed in PR #1234',
          }}
          onConfirm={(note) => {
            const target = dialog;
            setDialog(null);
            if (note && note.trim()) confirmDispose(target, note.trim());
          }}
          onCancel={() => setDialog(null)}
        />
      )}
    </div>
  );
}
