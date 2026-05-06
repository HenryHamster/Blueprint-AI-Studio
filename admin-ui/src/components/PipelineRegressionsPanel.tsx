import { useState, useEffect, useCallback } from 'react';
import { AlertTriangle, CheckCircle, RotateCcw } from 'lucide-react';
import { apiFetch } from '../api';
import { timeAgo } from '../utils/time';

interface FixLogEntry {
  fix_pr_number?: number;
  merged_at?: string;
  recovered_at?: string;
  post_merge_value?: number;
}

interface ManualInterventionCase {
  id: string;
  regression_class: string;
  signal_name: string;
  status: string;
  attempts: number;
  last_remediation_pr_number: number | null;
  last_failure_class: string | null;
  signal_snapshot_at_spawn: Record<string, unknown> | null;
  signal_snapshot_at_merge: Record<string, unknown> | null;
  fingerprint_to_fix_log: FixLogEntry[];
  last_seen_at: string | null;
  needs_manual_intervention_at: string | null;
  signal_first_seen_at: string | null;
  time_to_detect_sec: number | null;
  escalation_reason: string | null;
}

const ESCALATION_REASON_LABELS: Record<string, string> = {
  investigations_diverged: 'Diverged diagnoses',
  attempts_exhausted: 'Max attempts',
  stale_active: 'Stale (watchdog)',
  stale_merged: 'Stale (watchdog)',
};

interface Props {
  projectId: string;
}

function formatMttd(sec: number | null): string {
  if (sec === null || sec < 0) return '—';
  if (sec < 60) return `${Math.round(sec)}s`;
  if (sec < 3600) return `${Math.round(sec / 60)}m`;
  return `${(sec / 3600).toFixed(1)}h`;
}

function snapshotSummary(snap: Record<string, unknown> | null): string {
  if (!snap) return '—';
  const entries = Object.entries(snap)
    .filter(([, v]) => typeof v === 'number' || typeof v === 'string')
    .slice(0, 3)
    .map(([k, v]) => `${k.replace(/_/g, ' ')}: ${typeof v === 'number' ? Math.round(Number(v) * 100) / 100 : v}`);
  return entries.length ? entries.join(', ') : '—';
}

export function ManualInterventionPanel({ projectId }: Props) {
  const [cases, setCases] = useState<ManualInterventionCase[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [clearing, setClearing] = useState<string | null>(null);

  const fetchCases = useCallback(async () => {
    try {
      const res = await apiFetch(
        `/api/admin/regression-cases/manual-intervention?project_id=${encodeURIComponent(projectId)}`
      );
      if (res.ok) {
        const data = await res.json();
        setCases(data.cases ?? []);
        setLoaded(true);
      } else if (res.status !== 200) {
        setLoaded(true);
      }
    } catch { /* silent */ }
  }, [projectId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch sets state after await, intentional
    fetchCases();
    const id = setInterval(fetchCases, 60_000);
    return () => clearInterval(id);
  }, [fetchCases]);

  const handleClear = useCallback(async (regressionClass: string) => {
    setClearing(regressionClass);
    try {
      const res = await apiFetch(
        `/api/admin/regression-cases/${encodeURIComponent(regressionClass)}/clear-manual-intervention?project_id=${encodeURIComponent(projectId)}`,
        { method: 'POST' }
      );
      if (res.ok) {
        setCases(prev => prev.filter(c => c.regression_class !== regressionClass));
      }
    } catch { /* silent */ }
    setClearing(null);
  }, [projectId]);

  if (!loaded) return null;

  return (
    <section data-testid="manual-intervention-panel">
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">
        Manual Intervention Required
        {cases.length > 0 && (
          <span className="ml-2 bg-red-500/20 text-red-400 text-[10px] font-semibold px-1.5 py-0.5 rounded-full normal-case tracking-normal">
            {cases.length}
          </span>
        )}
      </h2>
      {cases.length === 0 ? (
        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 flex items-center gap-2 text-emerald-400 text-xs">
          <CheckCircle className="w-4 h-4 shrink-0" />
          No regression cases need manual intervention
        </div>
      ) : (
        <div className="space-y-2">
          {cases.map(c => (
            <div
              key={c.id}
              className="bg-red-950/20 border border-red-900/40 border-l-2 border-l-red-500 rounded-lg px-3 py-2"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <AlertTriangle className="w-3.5 h-3.5 text-red-400 shrink-0" />
                    <span className="text-xs text-red-300 font-medium capitalize">
                      {c.signal_name}
                    </span>
                    <span className="text-[10px] text-neutral-500 font-mono">{c.regression_class}</span>
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-red-500/10 text-red-400 border border-red-500/20">
                      {c.attempts} attempt{c.attempts !== 1 ? 's' : ''}
                    </span>
                    {c.escalation_reason && ESCALATION_REASON_LABELS[c.escalation_reason] && (
                      <span
                        className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-300 border border-amber-500/20"
                        title={`escalation_reason: ${c.escalation_reason}`}
                      >
                        {ESCALATION_REASON_LABELS[c.escalation_reason]}
                      </span>
                    )}
                  </div>
                  <div className="mt-1.5 space-y-0.5">
                    <div className="text-[10px] text-neutral-500">
                      <span className="text-neutral-400 font-medium">At spawn:</span>{' '}
                      {snapshotSummary(c.signal_snapshot_at_spawn)}
                    </div>
                    <div className="text-[10px] text-neutral-500">
                      <span className="text-neutral-400 font-medium">At merge:</span>{' '}
                      {snapshotSummary(c.signal_snapshot_at_merge)}
                    </div>
                    {c.fingerprint_to_fix_log.length > 0 && (
                      <div className="mt-1.5 border-t border-red-900/30 pt-1">
                        <div className="text-[10px] text-neutral-400 font-medium mb-0.5">Prior attempts:</div>
                        <div className="space-y-0.5">
                          {c.fingerprint_to_fix_log.map((entry, idx) => (
                            <div key={idx} className="text-[9px] text-neutral-500">
                              {entry.fix_pr_number && entry.merged_at && (
                                <span>
                                  PR #{entry.fix_pr_number} merged {new Date(entry.merged_at).toLocaleDateString()}
                                  {entry.recovered_at && ' → recovered'}
                                </span>
                              )}
                              {entry.recovered_at && !entry.fix_pr_number && (
                                <span>Recovered {new Date(entry.recovered_at).toLocaleDateString()}</span>
                              )}
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                    <div className="flex items-center gap-3 mt-1">
                      {c.last_remediation_pr_number != null && (
                        <span className="text-[10px] text-neutral-400">
                          Last PR: #{c.last_remediation_pr_number}
                        </span>
                      )}
                      {c.last_failure_class && (
                        <span className="text-[10px] text-neutral-500 font-mono">
                          last fail: {c.last_failure_class}
                        </span>
                      )}
                      {c.last_seen_at && (
                        <span className="text-[10px] text-neutral-500">
                          last seen {timeAgo(c.last_seen_at)}
                        </span>
                      )}
                      {c.time_to_detect_sec !== null && c.time_to_detect_sec !== undefined && (
                        <span className="text-[10px] text-amber-500/80" title="Time from signal crossing threshold to case opening (MTTD)">
                          MTTD {formatMttd(c.time_to_detect_sec)}
                        </span>
                      )}
                    </div>
                  </div>
                </div>
                <button
                  onClick={() => handleClear(c.regression_class)}
                  disabled={clearing === c.regression_class}
                  className="shrink-0 flex items-center gap-1.5 text-[10px] px-2 py-1 rounded border border-neutral-700 text-neutral-300 hover:text-white hover:border-neutral-500 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                  title="Clear manual intervention and re-arm autonomous remediation"
                >
                  <RotateCcw className={`w-3 h-3 ${clearing === c.regression_class ? 'animate-spin' : ''}`} />
                  Clear &amp; re-arm
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
