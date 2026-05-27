import type { QualificationVerdictRecord, CurrentVersion, AuditReconciliation } from '../api';

interface ProjectReleaseQualificationSectionProps {
  currentVersion: CurrentVersion | null;
  verdicts: QualificationVerdictRecord[];
  loading?: boolean;
}

function SubScoreBadge({ label, value }: { label: string; value: number | null | undefined }) {
  if (value == null) {
    return (
      <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-500 border border-neutral-700">
        {label} <span className="font-mono">—</span>
      </span>
    );
  }
  const pct = Math.round(value * 100);
  const color =
    value >= 0.7 ? 'bg-emerald-950/40 text-emerald-400 border-emerald-900/50' :
    value >= 0.4 ? 'bg-amber-950/40 text-amber-400 border-amber-900/50' :
                   'bg-red-950/40 text-red-400 border-red-900/50';
  return (
    <span className={`inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded border ${color}`}>
      {label} <span className="font-mono">{pct}%</span>
    </span>
  );
}

function AuditReconciliationRow({ rec }: { rec: AuditReconciliation }) {
  const hasDivergence = rec.score_delta != null && rec.score_delta >= 0.1;
  return (
    <div className="flex flex-wrap gap-1.5 items-center pt-0.5">
      <span className="text-[10px] text-neutral-600 shrink-0">audit reconciliation:</span>
      {rec.model_reported_score != null && (
        <span className="text-[10px] px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-400 border border-neutral-700 font-mono">
          model {Math.round(rec.model_reported_score * 100)}%
        </span>
      )}
      {rec.findings_derived_score != null && (
        <span className="text-[10px] px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-400 border border-neutral-700 font-mono">
          findings {Math.round(rec.findings_derived_score * 100)}%
        </span>
      )}
      {rec.score_delta != null && (
        <span className={`text-[10px] px-1.5 py-0.5 rounded border font-mono ${
          hasDivergence ? 'bg-amber-950/40 text-amber-400 border-amber-900/50' : 'bg-neutral-800 text-neutral-500 border-neutral-700'
        }`}>
          δ {Math.round(rec.score_delta * 100)}%
        </span>
      )}
      {rec.excluded_findings_count > 0 && (
        <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-950/40 text-amber-500 border border-amber-900/50">
          {rec.excluded_findings_count} finding{rec.excluded_findings_count !== 1 ? 's' : ''} excluded
        </span>
      )}
    </div>
  );
}

function VerdictRow({ v }: { v: QualificationVerdictRecord }) {
  const shortSha = v.candidate_sha ? v.candidate_sha.slice(0, 8) : '—';
  const when = v.finalized_at ? new Date(v.finalized_at).toLocaleString() : '—';
  const ss = v.sub_scores;

  return (
    <div className="bg-neutral-900 border border-neutral-800 rounded-lg p-3 space-y-2">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="font-mono text-xs text-neutral-200">{shortSha}</span>
        <span className="text-[10px] text-neutral-500">{when}</span>
        {v.would_promote === true && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-950/40 text-emerald-400 border border-emerald-900/50">would promote</span>
        )}
        {v.would_promote === false && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-500 border border-neutral-700">no promote</span>
        )}
        {v.blocker && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-red-950/40 text-red-400 border border-red-900/50 truncate max-w-[160px]" title={v.blocker}>{v.blocker}</span>
        )}
      </div>
      <div className="flex flex-wrap gap-1.5">
        <SubScoreBadge label="audit" value={ss?.audit} />
        {v.audit_missing_reason && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-950/40 text-amber-500 border border-amber-900/50 truncate max-w-[160px]" title={v.audit_missing_reason}>{v.audit_missing_reason}</span>
        )}
        <SubScoreBadge label="smoketest" value={ss?.smoketest} />
        <SubScoreBadge label="test_erosion" value={ss?.test_erosion} />
        {v.terminal_status === 'infra_failure' && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-950/40 text-amber-400 border border-amber-900/50">infra_failure</span>
        )}
      </div>
    </div>
  );
}

export function ProjectReleaseQualificationSection({
  currentVersion,
  verdicts,
  loading,
}: ProjectReleaseQualificationSectionProps) {
  const lq = currentVersion?.last_qualification;
  const infraErrors = lq?.consecutive_infra_errors ?? 0;

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Release Qualification</h3>
        <p className="text-[11px] text-neutral-600 mb-3">
          Recent qualification runs for this project. Sub-scores are green ≥ 70%, amber ≥ 40%, red below 40%.
        </p>

        {currentVersion && (
          <div className="bg-neutral-900 border border-neutral-800 rounded-lg p-3 mb-4 space-y-2">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-xs text-neutral-400 shrink-0 w-24">Phase</span>
              <span className="font-mono text-xs text-neutral-200">{currentVersion.phase}</span>
            </div>
            {lq && (
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-xs text-neutral-400 shrink-0 w-24">Last run</span>
                <span className={`text-[10px] px-1.5 py-0.5 rounded border font-mono ${
                  infraErrors > 0 || lq.error
                    ? 'bg-amber-950/40 text-amber-400 border-amber-900/50'
                    : lq.status === 'promoted'
                    ? 'bg-emerald-950/40 text-emerald-400 border-emerald-900/50'
                    : 'bg-neutral-800 text-neutral-400 border-neutral-700'
                }`}>{lq.status ?? '—'}</span>
                {lq.error && (
                  <span className="text-[10px] text-red-400 truncate max-w-[200px]" title={lq.error}>{lq.error}</span>
                )}
              </div>
            )}
            {infraErrors > 0 && (
              <div className="flex items-center gap-2 mt-1 px-2 py-1.5 rounded bg-amber-950/30 border border-amber-900/40">
                <span className="text-[11px] text-amber-400">&#9888; {infraErrors} consecutive infra error{infraErrors !== 1 ? 's' : ''}</span>
              </div>
            )}
            {lq?.audit_reconciliation && (
              <AuditReconciliationRow rec={lq.audit_reconciliation} />
            )}
          </div>
        )}

        {loading && (
          <p className="text-xs text-neutral-500">Loading verdicts&hellip;</p>
        )}

        {!loading && verdicts.length === 0 && (
          <p className="text-xs text-neutral-600">No qualification verdicts recorded yet.</p>
        )}

        {!loading && verdicts.length > 0 && (
          <div className="space-y-2">
            {verdicts.map(v => <VerdictRow key={v.id} v={v} />)}
          </div>
        )}
      </div>
    </div>
  );
}
