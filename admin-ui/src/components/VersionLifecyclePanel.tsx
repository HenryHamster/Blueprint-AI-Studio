import { useState, useEffect, useRef } from 'react';
import type { JSX } from 'react';
import {
  clearDeployFailed,
  cutVersion,
  fetchQualificationVerdicts,
  patchAdminSettings,
  setProjectPromotionEnabled,
  setVersionPhase,
  setVersionGoal,
  triggerQualify,
  type AuditFinding,
  type AuditReconciliation,
  type ChallengerRefutedFinding,
  type CiResolution,
  type CurrentVersion,
  type LatestAudit,
  type QualificationVerdictRecord,
  type ReleaseArtifactEvidence,
  type StagedDirection,
  type VersionLifecycleResponse,
  type VersionQualification,
  type PromotionReadiness,
  type QualificationState,
  type ChallengedVsUnchallengedTelemetry,
} from '../api';
import { timeAgo, formatDurationSec, deployWindowStatus } from '../utils/time';

// Generate a client idempotency key for a version cut. Prefers the native UUID
// generator and falls back to a timestamp+random token for older runtimes (and
// JSDOM, where crypto.randomUUID may be absent). Mirrors MessagesPanel.
function generateIdempotencyKey(): string {
  const c = typeof globalThis !== 'undefined' ? globalThis.crypto : undefined;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  return `idem-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

const QUAL_VERDICT_COLOR: Record<string, string> = {
  pending: 'text-blue-400',
  promoted: 'text-emerald-400',
  dry_run: 'text-blue-400',
  cached_verdict: 'text-blue-400',
  not_promoted: 'text-amber-400',
  promotion_failed: 'text-red-400',
  error: 'text-red-400',
  sha_error: 'text-red-400',
};

// Badge showing the real-world outcome of a promoted verdict: whether the
// promotion that was gated actually served in production (serving_confirmed),
// failed to deploy (deploy_failed), or has not yet resolved (awaiting).
function PostPromotionOutcomeBadge({
  outcome,
  outcomeAt,
}: {
  outcome: string | null | undefined;
  outcomeAt?: string | null;
}) {
  if (outcome === 'serving_confirmed') {
    return (
      <span
        data-testid="post-promotion-outcome-badge"
        data-outcome="serving_confirmed"
        className="font-mono text-[9px] px-1 py-0.5 rounded border bg-emerald-500/10 border-emerald-500/30 text-emerald-400"
        title={outcomeAt ? `serving confirmed ${timeAgo(outcomeAt)}` : 'serving confirmed post-promotion'}
      >
        ✓ serving
      </span>
    );
  }
  if (outcome === 'deploy_failed') {
    return (
      <span
        data-testid="post-promotion-outcome-badge"
        data-outcome="deploy_failed"
        className="font-mono text-[9px] px-1 py-0.5 rounded border bg-red-500/10 border-red-500/40 text-red-400"
        title={outcomeAt ? `deploy failed ${timeAgo(outcomeAt)}` : 'deploy failed post-promotion'}
      >
        ⚠ deploy failed
      </span>
    );
  }
  return (
    <span
      data-testid="post-promotion-outcome-badge"
      data-outcome="awaiting"
      className="font-mono text-[9px] px-1 py-0.5 rounded border bg-amber-500/10 border-amber-500/30 text-amber-400"
      title="post-promotion outcome not yet recorded"
    >
      awaiting outcome
    </span>
  );
}

function QualSummary({ q }: { q: VersionQualification | null }) {
  if (!q || q.confidence == null) return <span className="text-neutral-500 text-[10px]">—</span>;
  const conf = `${Math.round(q.confidence * 100)}%`;
  const thr = q.threshold != null ? `/${Math.round(q.threshold * 100)}%` : '';
  const status = q.status ?? 'unknown';
  return (
    <span className={`font-mono text-[10px] ${QUAL_VERDICT_COLOR[status] ?? 'text-neutral-400'}`}>
      {conf}{thr} {status.replace(/_/g, '-')}
    </span>
  );
}

const CI_STATE_STYLE: Record<string, string> = {
  green: 'text-emerald-400',
  red: 'text-red-400',
  pending: 'text-amber-400',
  no_checks: 'text-neutral-500',
};

function CiBadge({ ci }: { ci: CiResolution }) {
  const color = CI_STATE_STYLE[ci.state] ?? 'text-neutral-500';
  return (
    <span className={`font-mono text-[9px] ${color}`} title={ci.detail}>
      ci:{ci.state}{ci.resolved_sha ? ` @${ci.resolved_sha.slice(0, 7)}` : ''}
    </span>
  );
}

const AUDIT_MISSING_REASON_LABELS: Record<string, string> = {
  no_findings_key: 'audit findings key missing',
  empty_findings_list: 'audit findings list empty',
  audit_score_null_no_findings: 'audit score missing with no findings',
  parseable_but_unscoreable: 'audit findings present but missing severity tags',
};

const FINDING_SEVERITY_STYLE: Record<string, string> = {
  critical: 'bg-red-500/10 text-red-400 border-red-500/20',
  high: 'bg-orange-500/10 text-orange-400 border-orange-500/20',
  medium: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
  low: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
};
const FINDING_SEVERITY_ORDER = ['critical', 'high', 'medium', 'low'];

// Operator-readable display state for an audit that carries no report yet,
// derived once from the underlying audit task status. Mirrors the backend's
// terminal-vs-non-terminal task-status notion so this never disagrees with the
// qualification-state summary rendered above (which itself splits a bare
// 'pending' into 'qualifying' / 'in flight'):
//   - non-terminal (QUEUED/PROVISIONING/IN_PROGRESS/EVALUATING) → still running
//   - BLOCKED → waiting on a precondition, distinct from a crash
//   - FAILED/CANCELLED (or unknown) → genuinely crashed, loud red affordance
//   - COMPLETED with no report → finished-but-empty anomaly, not a crash
type AuditNoReportKind = 'running' | 'blocked' | 'crashed' | 'completed_empty';

const AUDIT_NO_REPORT_DISPLAY: Record<
  AuditNoReportKind,
  { label: string; className: string }
> = {
  running: {
    label: 'audit running — no report yet',
    className: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
  },
  blocked: {
    label: 'audit blocked — waiting on a precondition',
    className: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
  },
  crashed: {
    label: 'audit crashed — no report',
    className: 'bg-red-500/10 text-red-400 border-red-500/20',
  },
  completed_empty: {
    label: 'audit completed without a report',
    className: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
  },
};

function classifyAuditNoReport(taskStatus: string | null | undefined): AuditNoReportKind {
  switch ((taskStatus ?? '').toUpperCase()) {
    case 'QUEUED':
    case 'PROVISIONING':
    case 'IN_PROGRESS':
    case 'EVALUATING':
      return 'running';
    case 'BLOCKED':
      return 'blocked';
    case 'COMPLETED':
      return 'completed_empty';
    case 'FAILED':
    case 'CANCELLED':
    default:
      return 'crashed';
  }
}
const QUAL_STATE_STYLE: Record<string, string> = {
  qualifying: 'bg-amber-500/10 border-amber-500/30 text-amber-400',
  // The audit child crashed (terminal-failed, no report) while the verdict is
  // still pending — loud red, NOT the amber 'qualifying' look. Keeps the top
  // badge in agreement with the audit-findings section's 'audit crashed' cue.
  audit_crashed: 'bg-red-500/10 border-red-500/30 text-red-400',
  promotion_in_flight: 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400',
  promotion_failed: 'bg-red-500/10 border-red-500/30 text-red-400',
  dry_run: 'bg-blue-500/10 border-blue-500/30 text-blue-400',
  promoted: 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400',
  // Promotion is merged but serving is not yet confirmed — provisional, amber.
  promoted_unconfirmed: 'bg-amber-500/10 border-amber-500/30 text-amber-400',
  // Promotion merged but the deploy never confirmed serving — loud red, NOT the
  // clean green 'promoted' look.
  promotion_deploy_failed: 'bg-red-500/10 border-red-500/30 text-red-400',
  // Promotion merged but serving confirmation TIMED OUT with no active failure —
  // unverified, not failed. Amber (caution), distinct from the red failed look.
  promotion_confirmation_timed_out: 'bg-amber-500/10 border-amber-500/30 text-amber-400',
  not_promoted: 'bg-amber-500/10 border-amber-500/30 text-amber-400',
  deferred_unsynced: 'bg-amber-500/10 border-amber-500/30 text-amber-400',
};

type PromotionFreshnessContext = {
  detailText: string;
  toneClassName: string;
};

function finiteSeconds(value: number | null | undefined): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  return Math.max(0, value);
}

function formatPromotionFreshnessContext(
  state: QualificationState | null | undefined,
): PromotionFreshnessContext | null {
  const ageSeconds = finiteSeconds(state?.promotion_age_seconds);
  if (ageSeconds == null) return null;
  const timeoutSeconds = finiteSeconds(state?.promotion_timeout_seconds);
  let detailText = `in flight ~${formatDurationSec(ageSeconds)}`;
  if (timeoutSeconds != null && timeoutSeconds > 0) {
    detailText += ` (auto-timeout cap ${formatDurationSec(timeoutSeconds)})`;
  }
  if (state?.promotion_past_timeout) {
    return {
      detailText: `${detailText}, past auto-timeout`,
      toneClassName: 'text-red-400',
    };
  }
  if (state?.promotion_nearing_timeout) {
    return {
      detailText: `${detailText}, nearing auto-timeout`,
      toneClassName: 'text-amber-400',
    };
  }
  return {
    detailText,
    toneClassName: 'text-emerald-300',
  };
}

// Distinct, operator-readable serving-confirmation badge for a promoted version.
// Green = confirmed live; amber = still awaiting deploy confirmation; red = a
// deploy that failed to stabilize (with the failure reason). Renders nothing
// when there is no serving signal (e.g. a not-yet-promoted version).
function ServingStatusBadge({
  servingStatus,
  servedSha,
  servedAt,
  deployFailureReason,
  deployConfirmDeadline,
}: {
  servingStatus?: string | null;
  servedSha?: string | null;
  servedAt?: string | null;
  deployFailureReason?: string | null;
  deployConfirmDeadline?: string | null;
}) {
  if (!servingStatus) return null;
  if (servingStatus === 'serving_confirmed') {
    return (
      <span
        data-testid="serving-status-badge"
        data-serving-status="serving_confirmed"
        className="inline-flex items-center gap-1 text-[10px] font-semibold px-1.5 py-0.5 rounded border bg-emerald-500/10 border-emerald-500/30 text-emerald-400"
        title="The promoted commit is confirmed serving in production."
      >
        ✓ serving confirmed
        {servedSha && <span className="font-mono text-emerald-500/90">@{servedSha.slice(0, 7)}</span>}
        {servedAt && <span className="text-emerald-600/90">{timeAgo(servedAt)}</span>}
      </span>
    );
  }
  if (servingStatus === 'deploy_failed') {
    return (
      <span
        data-testid="serving-status-badge"
        data-serving-status="deploy_failed"
        className="inline-flex items-center gap-1 text-[10px] font-semibold px-1.5 py-0.5 rounded border bg-red-500/10 border-red-500/40 text-red-400"
        title={deployFailureReason ?? 'The promoted deploy never confirmed serving in production.'}
      >
        ⚠ deploy failed — NOT serving
        {deployFailureReason && (
          <span className="font-normal text-red-300 truncate max-w-[200px]">{deployFailureReason}</span>
        )}
      </span>
    );
  }
  if (servingStatus === 'confirmation_timed_out') {
    return (
      <span
        data-testid="serving-status-badge"
        data-serving-status="confirmation_timed_out"
        className="inline-flex items-center gap-1 text-[10px] font-semibold px-1.5 py-0.5 rounded border bg-amber-500/10 border-amber-500/40 text-amber-400"
        title={
          deployFailureReason
            ? `Promoted, but serving confirmation timed out (${deployFailureReason}). No active deploy failure was observed — the promoted commit just could not be confirmed serving in time. Verify serving; it auto-recovers if later observed live.`
            : 'Promoted, but serving confirmation timed out — no active deploy failure was observed. Verify serving; it auto-recovers if later observed live.'
        }
      >
        ⧖ confirmation timed out — unverified
        {deployFailureReason && (
          <span className="font-normal text-amber-300 truncate max-w-[200px]">{deployFailureReason}</span>
        )}
      </span>
    );
  }
  if (servingStatus === 'unconfirmed') {
    // Compute the confirmation-window state at render time so the panel is
    // honest at view time, not at fetch time. When `now` is past the deadline
    // while serving is still unconfirmed, the resolving sweep is late/stuck and
    // the deploy should be investigated — render a distinct overdue cue.
    let windowText: string | null = null;
    let overdue = false;
    if (deployConfirmDeadline) {
      const status = deployWindowStatus(deployConfirmDeadline);
      overdue = status.overdue;
      windowText = status.text;
    }
    return (
      <span
        data-testid="serving-status-badge"
        data-serving-status="unconfirmed"
        data-deploy-window-overdue={deployConfirmDeadline ? String(overdue) : undefined}
        className={`inline-flex items-center gap-1 text-[10px] font-semibold px-1.5 py-0.5 rounded border ${
          overdue
            ? 'bg-red-500/10 border-red-500/40 text-red-400'
            : 'bg-amber-500/10 border-amber-500/30 text-amber-400'
        }`}
        title={
          overdue
            ? 'Promoted (merged) but serving is still not confirmed and the deploy confirmation window has elapsed — the resolving sweep is late or stuck; investigate the deploy.'
            : 'Promoted (merged) but serving is not yet confirmed — still within the deploy confirmation window.'
        }
      >
        {overdue ? '⚠ awaiting deploy confirmation — OVERDUE' : '… awaiting deploy confirmation'}
        {windowText && (
          <span className={`font-normal ${overdue ? 'text-red-300' : 'text-amber-500/90'}`}>{windowText}</span>
        )}
      </span>
    );
  }
  return null;
}

// Per-artifact serving label/colour for the evidence breakdown. The backend
// emits serving_status as serving_confirmed | deploy_failed | unconfirmed | null;
// a confirmed artifact that follows a prior failure reads as 'recovered'.
const ARTIFACT_STATUS_STYLE: Record<string, { cls: string; label: string }> = {
  serving_confirmed: { cls: 'text-emerald-400', label: '✓ serving' },
  deploy_failed: { cls: 'text-red-400', label: '⚠ failed' },
  confirmation_timed_out: { cls: 'text-amber-400', label: '⧖ timed out' },
  unconfirmed: { cls: 'text-amber-400', label: '… pending' },
};

// Per-artifact serving evidence so an operator can see WHICH registered deploy
// artifact is pending/failed/recovered behind the aggregate headline. Renders
// nothing for the legacy single-artifact case (or when the API omits the list),
// so a backend-only response looks exactly as it did before this breakdown.
function ArtifactEvidenceList({ artifacts }: { artifacts?: ReleaseArtifactEvidence[] | null }) {
  if (!artifacts || artifacts.length <= 1) return null;
  return (
    <div
      data-testid="artifact-evidence-list"
      className="flex flex-col gap-0.5 pl-2 border-l border-neutral-700/50"
    >
      {artifacts.map((a) => {
        const style =
          ARTIFACT_STATUS_STYLE[a.serving_status ?? ''] ??
          { cls: 'text-neutral-500', label: a.serving_status ?? 'n/a' };
        return (
          <div
            key={a.artifact_kind}
            data-testid="artifact-evidence-row"
            data-artifact-kind={a.artifact_kind}
            data-artifact-status={a.serving_status ?? 'none'}
            className="flex flex-wrap items-center gap-1.5 text-[9px]"
          >
            <span className="font-mono text-neutral-400">{a.artifact_kind}</span>
            {a.required && <span className="text-neutral-600 uppercase tracking-wide">required</span>}
            <span className={`font-semibold ${style.cls}`}>{style.label}</span>
            {a.served_identifier && (
              <span className="font-mono text-neutral-500 truncate max-w-[160px]">{a.served_identifier}</span>
            )}
            {a.served_at && <span className="text-neutral-600">{timeAgo(a.served_at)}</span>}
            {a.reason_code && (
              <span className="text-red-300/80 truncate max-w-[180px]" title={a.reason_code}>{a.reason_code}</span>
            )}
          </div>
        );
      })}
    </div>
  );
}

// Provenance for scoped audit evidence. Shows which commit the audit ran
// against and how long ago; the backend decides whether that evidence is
// current or merely historical context for this candidate.
function AuditProvenance({
  audit,
}: {
  audit: LatestAudit;
}) {
  if (!audit.candidate_sha && !audit.created_at) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {audit.candidate_sha && (
        <span className="text-[10px] text-neutral-500 font-mono" title={audit.candidate_sha}>
          @{audit.candidate_sha.slice(0, 7)}
        </span>
      )}
      {audit.created_at && (
        <span className="text-[10px] text-neutral-600">ran {timeAgo(audit.created_at)}</span>
      )}
    </div>
  );
}

function AuditFindingsList({ audit }: { audit: LatestAudit }) {
  if (!audit.report) {
    const { label, className } = AUDIT_NO_REPORT_DISPLAY[classifyAuditNoReport(audit.task_status)];
    return (
      <div className="space-y-0.5">
        <AuditProvenance audit={audit} />
        <span className={`text-[10px] px-1.5 py-0.5 rounded border ${className}`}>
          {label}
        </span>
      </div>
    );
  }
  const findings: AuditFinding[] = audit.report.findings ?? [];
  if (findings.length === 0) {
    return (
      <div className="space-y-0.5">
        <AuditProvenance audit={audit} />
        <span className="text-[10px] text-neutral-500 italic">audit: no findings</span>
      </div>
    );
  }
  const sorted = [...findings].sort((a, b) => {
    const ai = FINDING_SEVERITY_ORDER.indexOf(a.severity?.toLowerCase() ?? '');
    const bi = FINDING_SEVERITY_ORDER.indexOf(b.severity?.toLowerCase() ?? '');
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });
  return (
    <div className="space-y-0.5">
      <p className="text-[10px] text-neutral-500 uppercase tracking-wide">Audit Findings</p>
      <AuditProvenance audit={audit} />
      {sorted.map((f, i) => (
        <div key={i} className={`flex items-start gap-1.5 text-[10px] px-1.5 py-0.5 rounded border ${FINDING_SEVERITY_STYLE[f.severity?.toLowerCase() ?? ''] ?? 'bg-neutral-800/50 text-neutral-400 border-neutral-700/50'}`}>
          <span className="font-semibold shrink-0">{f.severity ?? '?'}</span>
          <span>{f.summary}</span>
        </div>
      ))}
    </div>
  );
}

// Stale-audit view: the newest scoped audit ran against an older candidate than
// the one now under evaluation. When the active candidate SHA is known, contrast
// it explicitly against the audited commit so an operator can see exactly which
// commit is waiting for a fresh audit and which one the stale findings describe.
function HistoricalAuditContext({
  audit,
  activeCandidateSha,
}: {
  audit: LatestAudit;
  activeCandidateSha?: string | null;
}) {
  const auditedShort = audit.candidate_sha ? `@${audit.candidate_sha.slice(0, 7)}` : null;
  return (
    <div data-testid="historical-audit-context" className="space-y-0.5">
      <p className="text-[10px] text-neutral-500 uppercase tracking-wide">Historical Audit Context</p>
      <span className="text-[10px] px-1.5 py-0.5 rounded border bg-amber-500/10 text-amber-400 border-amber-500/20">
        no current audit for this candidate
      </span>
      {activeCandidateSha ? (
        <div className="flex flex-wrap items-center gap-1.5 text-[10px]">
          <span className="text-neutral-400">active</span>
          <span className="font-mono text-neutral-300" title={activeCandidateSha}>
            @{activeCandidateSha.slice(0, 7)}
          </span>
          <span className="text-neutral-600">waiting for a fresh audit · audited</span>
          {auditedShort && (
            <span className="font-mono text-neutral-500" title={audit.candidate_sha ?? undefined}>
              {auditedShort}
            </span>
          )}
          {audit.created_at && (
            <span className="text-neutral-600">ran {timeAgo(audit.created_at)}</span>
          )}
        </div>
      ) : (
        <AuditProvenance audit={audit} />
      )}
    </div>
  );
}

function AuditReconciliationRow({ rec }: { rec: AuditReconciliation }) {
  const hasDivergence = rec.score_delta != null && rec.score_delta >= 0.1;
  return (
    <div className="flex flex-wrap gap-1.5 items-center pt-0.5">
      <span className="text-[10px] text-neutral-600 shrink-0">reconciliation:</span>
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
        <span className={`text-[10px] px-1.5 py-0.5 rounded border font-mono ${hasDivergence ? 'bg-amber-950/40 text-amber-400 border-amber-900/50' : 'bg-neutral-800 text-neutral-500 border-neutral-700'}`}>
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

type DurableQualificationEvidence = {
  audit_findings?: AuditFinding[] | null;
  excluded_audit_findings?: AuditFinding[] | null;
  challenger_refuted_audit_findings?: ChallengerRefutedFinding[] | null;
  migration_safety?: string | null;
  migration_safety_detail?: string | null;
  migration_apply_safety?: string | null;
  migration_apply_safety_detail?: string | null;
  test_erosion_detail?: Record<string, unknown> | null;
  smoketest_detail?: Record<string, unknown> | null;
  promotion_pr_url?: string | null;
  promotion_conflict?: boolean | null;
  promotion_error_detail?: string | null;
};

function formatCompactDetail(value: unknown): string {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  try {
    const text = JSON.stringify(value);
    return text.length > 180 ? `${text.slice(0, 177)}…` : text;
  } catch {
    return String(value);
  }
}

function DurableQualificationEvidenceRow({ q }: { q: DurableQualificationEvidence }) {
  const findings = q.audit_findings ?? [];
  const excludedFindings = q.excluded_audit_findings ?? [];
  const chips: JSX.Element[] = [];

  if (q.migration_safety || q.migration_safety_detail) {
    chips.push(
      <span
        key="migration-safety"
        className="text-[10px] px-1.5 py-0.5 rounded border bg-neutral-800 text-neutral-400 border-neutral-700"
        title={q.migration_safety_detail ?? undefined}
      >
        migration {q.migration_safety ?? 'unknown'}
      </span>,
    );
  }
  if (q.migration_apply_safety || q.migration_apply_safety_detail) {
    chips.push(
      <span
        key="migration-apply-safety"
        className="text-[10px] px-1.5 py-0.5 rounded border bg-neutral-800 text-neutral-400 border-neutral-700"
        title={q.migration_apply_safety_detail ?? undefined}
      >
        apply {q.migration_apply_safety ?? 'unknown'}
      </span>,
    );
  }
  if (findings.length > 0) {
    chips.push(
      <span
        key="audit-findings"
        className="text-[10px] px-1.5 py-0.5 rounded border bg-sky-500/10 text-sky-300 border-sky-500/20"
        title={findings.map((f) => `${f.severity}: ${f.summary}`).join(' | ')}
      >
        {findings.length} audit finding{findings.length !== 1 ? 's' : ''}
      </span>,
    );
  }
  if (excludedFindings.length > 0) {
    chips.push(
      <span
        key="excluded-audit-findings"
        className="text-[10px] px-1.5 py-0.5 rounded border bg-neutral-800 text-neutral-500 border-neutral-700"
        title={excludedFindings.map((f) => `${f.severity}: ${f.summary}`).join(' | ')}
      >
        {excludedFindings.length} excluded audit finding{excludedFindings.length !== 1 ? 's' : ''}
      </span>,
    );
  }
  const challengerRefuted = q.challenger_refuted_audit_findings ?? [];
  if (challengerRefuted.length > 0) {
    chips.push(
      <span
        key="challenger-refuted-findings"
        data-testid="challenger-refuted-findings"
        className="text-[10px] px-1.5 py-0.5 rounded border bg-violet-500/10 text-violet-300 border-violet-500/20"
        title={challengerRefuted.map((f) => `${f.severity}: ${f.summary} — ${f.challenger_reason}`).join(' | ')}
      >
        {challengerRefuted.length} challenger false positive{challengerRefuted.length !== 1 ? 's' : ''}
      </span>,
    );
  }
  if (q.test_erosion_detail) {
    chips.push(
      <span
        key="test-erosion-detail"
        className="text-[10px] px-1.5 py-0.5 rounded border bg-neutral-800 text-neutral-400 border-neutral-700"
        title={formatCompactDetail(q.test_erosion_detail)}
      >
        erosion {formatCompactDetail(q.test_erosion_detail)}
      </span>,
    );
  }
  if (q.smoketest_detail) {
    chips.push(
      <span
        key="smoketest-detail"
        className="text-[10px] px-1.5 py-0.5 rounded border bg-neutral-800 text-neutral-400 border-neutral-700"
        title={formatCompactDetail(q.smoketest_detail)}
      >
        smoke {formatCompactDetail(q.smoketest_detail)}
      </span>,
    );
  }
  if (q.promotion_pr_url) {
    chips.push(
      <span
        key="promotion-pr-url"
        className="text-[10px] px-1.5 py-0.5 rounded border bg-neutral-800 text-neutral-500 border-neutral-700"
        title={q.promotion_pr_url}
      >
        promotion PR
      </span>,
    );
  }
  if (q.promotion_conflict) {
    chips.push(
      <span
        key="promotion-conflict"
        className="text-[10px] px-1.5 py-0.5 rounded border bg-red-500/10 text-red-400 border-red-500/20"
        title={q.promotion_error_detail ?? 'promotion conflict'}
      >
        promotion conflict
      </span>,
    );
  } else if (q.promotion_error_detail) {
    chips.push(
      <span
        key="promotion-error-detail"
        className="text-[10px] px-1.5 py-0.5 rounded border bg-red-500/10 text-red-400 border-red-500/20"
        title={q.promotion_error_detail}
      >
        {q.promotion_error_detail}
      </span>,
    );
  }

  if (chips.length === 0) return null;

  return (
    <div data-testid="durable-qualification-evidence" className="flex flex-wrap gap-1.5 pt-0.5">
      {chips}
    </div>
  );
}


function PromotionReadinessSummary({ readiness }: { readiness: PromotionReadiness | null | undefined }) {
  if (!readiness) return null;
  const { ready, reason, verdicts_evaluated, would_promote_rate, consecutive_clean_passes, recurring_blockers, caveat, ground_truth_validated, validated_precision, resolved_outcome_sample } = readiness;
  const hasBlockers = Object.keys(recurring_blockers ?? {}).length > 0;
  // Once promotion is live, this dry-run window is frozen pre-go-live history —
  // it no longer receives verdicts, so it is NOT current readiness. Label it as a
  // historical calibration window rather than presenting a stale 'ready' number.
  const isHistoricalCalibration = readiness.is_current_readiness === false;
  if (isHistoricalCalibration) {
    return (
      <div className="mt-1 pt-1 border-t border-neutral-700/50 space-y-0.5">
        <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded border bg-neutral-500/10 border-neutral-500/30 text-neutral-400" title={reason}>
          pre-go-live dry-run calibration (historical)
        </span>
        <p className="text-[10px] text-neutral-500 line-clamp-2">{reason}</p>
        {verdicts_evaluated > 0 && (
          <p className="text-[9px] text-neutral-600">
            {verdicts_evaluated} pre-go-live dry-run{verdicts_evaluated !== 1 ? 's' : ''} · {consecutive_clean_passes} clean streak (frozen)
          </p>
        )}
      </div>
    );
  }
  // Determine the caveat text: use backend-provided caveat if available, otherwise default
  const explanatoryCaveat = caveat ?? (ground_truth_validated ? 'validated against post-promotion outcomes' : 'self-approval only — no post-promotion outcome is recorded');
  const hasValidatedPrecision = ground_truth_validated && validated_precision != null && (resolved_outcome_sample ?? 0) > 0;

  return (
    <div className="mt-1 pt-1 border-t border-neutral-700/50 space-y-0.5">
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border ${ready ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400' : 'bg-amber-500/10 border-amber-500/30 text-amber-400'}`}
          title={explanatoryCaveat}
        >
          {ready ? 'self-approval streak clears thresholds' : 'not ready'}
        </span>
        {verdicts_evaluated > 0 && (
          <span className="text-[10px] text-neutral-500">{verdicts_evaluated} dry-run{verdicts_evaluated !== 1 ? 's' : ''}</span>
        )}
        {verdicts_evaluated > 0 && (
          <span className="text-[10px] text-neutral-500" title={explanatoryCaveat}>{Math.round(would_promote_rate * 100)}% self-approval rate</span>
        )}
        {hasValidatedPrecision && (
          <span
            className="text-[10px] font-semibold px-1.5 py-0.5 rounded border bg-emerald-500/10 border-emerald-500/30 text-emerald-400"
            title={`validated qualifier precision from ${resolved_outcome_sample} resolved promotion outcomes`}
          >
            {Math.round(validated_precision * 100)}% validated precision ({resolved_outcome_sample} resolved)
          </span>
        )}
        {consecutive_clean_passes > 0 && (
          <span className="text-[10px] text-neutral-500">{consecutive_clean_passes} clean streak</span>
        )}
      </div>
      {ready && (
        <p className="text-[9px] text-neutral-600 italic">{explanatoryCaveat}</p>
      )}
      {!ready && reason && (
        <p className="text-[10px] text-neutral-400 line-clamp-2">{reason}</p>
      )}
      {hasBlockers && (
        <p className="text-[10px] text-red-400 truncate">
          recurring: {Object.entries(recurring_blockers).map(([k, n]) => `${k}×${n}`).join(', ')}
        </p>
      )}
    </div>
  );
}

function StagedDirectionsSummary({ staged }: { staged: StagedDirection[] }) {
  if (!staged.length) return null;
  return (
    <div data-testid="staged-directions">
      <p className="text-[10px] text-neutral-500 uppercase tracking-wide mb-1">
        Staged for next version ({staged.length})
      </p>
      <div className="space-y-0.5">
        {staged.map((s, i) => (
          <div key={s.id ?? i} className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] pl-2 border-l-2 border-neutral-700/40">
            {s.staged_for_version != null && (
              <span className="font-mono text-neutral-500 shrink-0">→v{s.staged_for_version}</span>
            )}
            {s.priority && <span className="text-neutral-600">{s.priority}</span>}
            <span className="text-neutral-300 truncate max-w-[220px]" title={s.text ?? undefined}>{s.text ?? '(no description)'}</span>
            {s.staged_age_days != null && (
              <span className="text-neutral-600">{s.staged_age_days}d staged</span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// Staged directions that a cut of `versionNumber` will activate NOW: those
// targeting this version or earlier, plus any with a missing target (the backend
// fails open and activates those too — see activate_eligible_staged_directions).
function stagedActivatedByCut(staged: StagedDirection[], versionNumber: number): StagedDirection[] {
  return staged.filter(
    (s) => s.staged_for_version == null || s.staged_for_version <= versionNumber,
  );
}

// Disclosure of a version cut's concrete effects, shown before the operator
// confirms: which staged directions begin running now and how much in-flight
// work keeps running against the freshly-opened version.
function CutConsequences({ cv, staged }: { cv: CurrentVersion; staged: StagedDirection[] }) {
  const activating = stagedActivatedByCut(staged, cv.version_number);
  const inFlight = cv.in_flight_task_count ?? 0;
  const qualState: QualificationState | null = cv.qualification_state ?? null;
  const promotionFreshness = formatPromotionFreshnessContext(qualState);
  return (
    <div
      data-testid="cut-consequences"
      className="text-[10px] text-neutral-300 bg-amber-950/30 border border-amber-900/40 rounded px-2 py-1 space-y-1"
    >
      <p className="text-amber-400 font-semibold">Cutting v{cv.version_number} now will:</p>
      {activating.length > 0 ? (
        <div className="space-y-0.5">
          <p>
            Start {activating.length} staged direction{activating.length !== 1 ? 's' : ''} immediately
            (rather than waiting for a clean promotion):
          </p>
          <ul className="list-disc pl-4 space-y-0.5">
            {activating.map((s, i) => (
              <li key={s.id ?? i} className="text-neutral-400 truncate max-w-[260px]" title={s.text ?? undefined}>
                {s.text ?? '(no description)'}
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="text-neutral-500">No staged directions will activate.</p>
      )}
      <p className={inFlight > 0 ? 'text-neutral-300' : 'text-neutral-500'}>
        {inFlight > 0
          ? `${inFlight} in-flight task${inFlight !== 1 ? 's' : ''} will NOT be stopped — they keep running and land against the new version.`
          : 'No tasks are currently in flight.'}
      </p>
      {qualState?.state === 'promotion_in_flight' && (
        <p className={promotionFreshness?.toneClassName ?? 'text-emerald-300'}>
          Promotion PR{qualState.promotion_pr_number ? ` #${qualState.promotion_pr_number}` : ''} is open and auto-merging
          {promotionFreshness ? ` (${promotionFreshness.detailText})` : ''} — this cut will be rejected until it lands or is canceled.
        </p>
      )}
    </div>
  );
}

function ChallengedVsUnchallengedSummary({ telemetry }: { telemetry: ChallengedVsUnchallengedTelemetry }) {
  const { challenged, unchallenged, total_verdicts } = telemetry;
  const pct = (r: number) => `${Math.round(r * 100)}%`;
  const bothInsufficient = challenged.insufficient_sample && unchallenged.insufficient_sample;
  return (
    <div data-testid="challenged-vs-unchallenged" className="mt-1 pt-1 border-t border-neutral-700/50 space-y-0.5">
      <p className="text-[10px] text-neutral-500 uppercase tracking-wide">Audit Challenge Impact ({total_verdicts} verdicts)</p>
      {bothInsufficient ? (
        <span className="text-[10px] text-neutral-600 italic">not enough data yet (need ≥3 per cohort)</span>
      ) : (
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          {([{ label: 'challenged', s: challenged }, { label: 'unchallenged', s: unchallenged }] as const).map(({ label, s }) => (
            <div key={label} className="space-y-0.5">
              <span className="text-[10px] text-neutral-500">{label} ({s.sample_size})</span>
              {s.insufficient_sample ? (
                <span className="text-[10px] text-neutral-600 italic"> — insufficient</span>
              ) : (
                <div className="flex flex-wrap gap-1">
                  <span className="text-[10px] px-1 py-0 rounded bg-neutral-800 border border-neutral-700 text-neutral-400" title="exclusion rate — fraction of verdicts with audit findings excluded for lacking verification">excl {pct(s.exclusion_rate ?? 0)}</span>
                  {s.disagreement_rate != null && (
                    <span className={`text-[10px] px-1 py-0 rounded border ${(s.disagreement_rate ?? 0) > 0.2 ? 'bg-amber-500/10 border-amber-500/30 text-amber-400' : 'bg-neutral-800 border-neutral-700 text-neutral-400'}`} title="challenger disagreement rate — fraction where the challenger did not fully agree">disagree {pct(s.disagreement_rate)}</span>
                  )}
                  {s.wrong_decision_rate != null && (
                    <span className={`text-[10px] px-1 py-0 rounded border ${s.wrong_decision_rate > 0 ? 'bg-red-500/10 border-red-500/30 text-red-400' : 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400'}`} title={`post-promotion wrong decision rate over ${s.wrong_decision_resolved_sample ?? 0} resolved promotions`}>wrong {pct(s.wrong_decision_rate)}</span>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function VersionLifecyclePanel({ data, loading, projectId, onRefresh, userRole }: { data: VersionLifecycleResponse | null; loading: boolean; projectId: string | null; onRefresh: () => void; userRole?: 'admin' | 'user' | null }) {
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [verdicts, setVerdicts] = useState<QualificationVerdictRecord[]>([]);
  const [editingGoal, setEditingGoal] = useState<boolean>(false);
  const [goalInput, setGoalInput] = useState<string>('');
  const [optimisticOverrides, setOptimisticOverrides] = useState<Partial<CurrentVersion> | null>(null);
  const [promotionEnabledOverride, setPromotionEnabledOverride] = useState<boolean | null>(null);
  const [cuttingVersion, setCuttingVersion] = useState<boolean>(false);
  const [cutReason, setCutReason] = useState<string>('');
  // One idempotency key per confirmed cut: generated on confirm, reused across
  // a lost-response retry, cleared on success so the next cut gets a fresh key.
  const cutIdempotencyKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (!projectId) { setVerdicts([]); return; }
    fetchQualificationVerdicts(projectId, 10).then(r => setVerdicts(r.verdicts)).catch(() => {});
  }, [projectId]);
  useEffect(() => {
    if (data?.current_version?.goal !== undefined) {
      setGoalInput(data.current_version.goal || '');
    }
  }, [data?.current_version?.goal]);
  useEffect(() => {
    setOptimisticOverrides(null);
  }, [data?.current_version?.phase, data?.current_version?.goal]);
  useEffect(() => {
    setPromotionEnabledOverride(null);
  }, [data?.promotion_enabled]);

  if (loading || !data || (!data.current_version && data.recent_versions.length === 0)) return null;
  const cv = data.current_version ? (optimisticOverrides ? { ...data.current_version, ...optimisticOverrides } : data.current_version) : null;
  const recent = data.recent_versions;
  const promotionEnabled = data.promotion_enabled ?? false;
  const optimisticPromotionEnabled = promotionEnabledOverride ?? promotionEnabled;
  const readiness = data.promotion_readiness;
  // A promotion in flight (audit passed, promotion PR open and auto-merging)
  // makes the qualify + phase-flip levers unsafe: re-qualifying clobbers the
  // promotion-pending record and a phase flip clears the in-flight indication
  // without cancelling anything. The backend refuses both with a 409; disable
  // the controls here so the panel presents the same truth, consistent with how
  // the cut lever already surfaces this state.
  const promotionInFlight = cv?.qualification_state?.state === 'promotion_in_flight';
  const promotionFreshness = formatPromotionFreshnessContext(cv?.qualification_state);
  const promotionLockContext = promotionFreshness ? ` (${promotionFreshness.detailText})` : '';
  const auditEvidenceState = cv?.audit_evidence_state ?? (cv?.latest_audit ? 'current' : 'none');
  // Admin-only lever: a promoted version latched to deploy_failed (current open
  // version after promotion, or a recent promoted one) can be cleared once the
  // deploy is verified or attested live. The backend endpoint is project-scoped
  // and targets the latest promoted deploy_failed version.
  const isAdmin = userRole === 'admin';
  // The clear lever is backed by a project-scoped endpoint that still targets
  // the latest promoted version latched to the scalar serving_status field.
  // Keep this predicate on that same backend-owned latch; aggregate-only
  // required-artifact failures still surface in the badges/breakdown, but the
  // clear endpoint cannot currently recover them directly.
  const hasDeployFailed = cv?.serving_status === 'deploy_failed'
    || recent.some(v => v.serving_status === 'deploy_failed');

  const handleSetPhase = async (phase: 'build' | 'stabilize') => {
    if (!projectId) return;
    setActionLoading('phase');
    setActionMsg(null);
    try {
      const response = await setVersionPhase(projectId, phase, 'operator advance');
      setOptimisticOverrides({
        phase: response.phase,
        phase_changed_at: response.phase_changed_at,
        phase_reasoning: response.phase_reasoning,
      });
      setActionMsg(`Phase → ${phase}`);
      onRefresh();
    } catch {
      setActionMsg('Request failed');
      setOptimisticOverrides(null);
    } finally {
      setActionLoading(null);
    }
  };

  const handleSetGoal = async () => {
    if (!projectId) return;
    setActionLoading('goal');
    setActionMsg(null);
    try {
      const response = await setVersionGoal(projectId, goalInput || null);
      setOptimisticOverrides({
        goal: response.goal,
        goal_updated_at: response.goal_updated_at,
        goal_editor_info: response.goal_editor_info,
      });
      setActionMsg('Goal updated');
      setEditingGoal(false);
      onRefresh();
    } catch {
      setActionMsg('Request failed');
      setOptimisticOverrides(null);
      setEditingGoal(false);
      setGoalInput(cv?.goal || '');
    } finally {
      setActionLoading(null);
    }
  };

  const handleQualify = async () => {
    if (!projectId) return;
    setActionLoading('qualify');
    setActionMsg(null);
    try {
      const result = await triggerQualify(projectId);
      setActionMsg(`qualify: ${result.status ?? 'done'}`);
      onRefresh();
    } catch {
      setActionMsg('Request failed');
    } finally {
      setActionLoading(null);
    }
  };

  const handleTogglePromotionEnabled = async () => {
    const nextValue = !optimisticPromotionEnabled;
    if (nextValue && !window.confirm('Enable live promotion for ALL PROJECTS? Passing qualify runs will push to main fleet-wide.')) return;
    setActionLoading('promotion');
    setActionMsg(null);
    setPromotionEnabledOverride(nextValue);
    try {
      await patchAdminSettings({ promotion_enabled: nextValue });
      setActionMsg(nextValue ? 'Live promotion enabled' : 'Live promotion disabled');
      onRefresh();
    } catch {
      setActionMsg('Request failed');
      setPromotionEnabledOverride(null);
    } finally {
      setActionLoading(null);
    }
  };

  const handleSetProjectPromotion = async (enabled: boolean | null) => {
    if (!projectId) return;
    if (enabled === true && !window.confirm('Arm live promotion for THIS project only? Passing qualify runs will push this project to main.')) return;
    setActionLoading('project-promotion');
    setActionMsg(null);
    try {
      await setProjectPromotionEnabled(projectId, enabled);
      setActionMsg(enabled === null ? 'Using fleet default' : enabled ? 'Project live promotion armed' : 'Project live promotion held');
      onRefresh();
    } catch {
      setActionMsg('Request failed');
    } finally {
      setActionLoading(null);
    }
  };

  const handleCutVersion = async () => {
    if (!projectId || !cv) return;
    const activating = stagedActivatedByCut(data?.staged_directions ?? [], cv.version_number);
    const inFlight = cv.in_flight_task_count ?? 0;
    const qualState: QualificationState | null = cv.qualification_state ?? null;
    const cutPromotionFreshness = formatPromotionFreshnessContext(qualState);
    const consequences = [
      activating.length > 0
        ? `${activating.length} staged direction${activating.length !== 1 ? 's' : ''} will start now`
        : null,
      inFlight > 0
        ? `${inFlight} in-flight task${inFlight !== 1 ? 's' : ''} will keep running against the new version`
        : null,
      qualState?.state === 'promotion_in_flight'
        ? `promotion PR${qualState.promotion_pr_number ? ` #${qualState.promotion_pr_number}` : ''} is open and auto-merging${cutPromotionFreshness ? ` (${cutPromotionFreshness.detailText})` : ''}`
        : null,
    ].filter(Boolean);
    const msg = consequences.length
      ? `Abandon v${cv.version_number} and open a fresh version?\n\nThis will: ${consequences.join('; ')}.`
      : `Abandon v${cv.version_number} and open a fresh version?`;
    if (!window.confirm(msg)) return;
    setActionLoading('cut');
    setActionMsg(null);
    if (!cutIdempotencyKeyRef.current) {
      cutIdempotencyKeyRef.current = generateIdempotencyKey();
    }
    try {
      const result = await cutVersion(projectId, cutReason || undefined, cutIdempotencyKeyRef.current);
      setActionMsg(`Cut v${result.abandoned_version.version_number} → opened v${result.new_version.version_number}`);
      setCuttingVersion(false);
      setCutReason('');
      cutIdempotencyKeyRef.current = null;
      onRefresh();
    } catch (error) {
      setActionMsg(error instanceof Error ? error.message : 'Request failed');
    } finally {
      setActionLoading(null);
    }
  };

  // Clear a stale post-promotion deploy_failed serving state. First attempts the
  // verify path (no force) — the backend clears only if the promoted SHA is
  // independently observed serving. If it refuses (live SHA unobservable), prompt
  // for an attestation reason and retry the operator-attested force path.
  const handleClearDeployFailed = async () => {
    if (!projectId) return;
    if (!window.confirm('Clear the stale deploy-failed serving state for the promoted version?\n\nThe deploy must be independently confirmed serving, or you will be asked to attest it is live.')) return;
    setActionLoading('clear-deploy-failed');
    setActionMsg(null);
    try {
      const result = await clearDeployFailed(projectId);
      setActionMsg(`Deploy-failed cleared (${result.clear_mode}) → ${result.serving_status ?? 'n/a'}`);
      onRefresh();
    } catch (verifyError) {
      const msg = verifyError instanceof Error ? verifyError.message : '';
      // Only offer the attested override when the backend explicitly asked for it
      // (the verify path could not confirm the deploy live).
      if (!msg.includes('force=true')) {
        setActionMsg(msg || 'Request failed');
        setActionLoading(null);
        return;
      }
      const reason = window.prompt('The deploy could not be independently verified live. To force-clear, enter an attestation reason (required):');
      if (!reason || !reason.trim()) {
        setActionMsg(msg);
        setActionLoading(null);
        return;
      }
      try {
        const result = await clearDeployFailed(projectId, true, reason.trim());
        setActionMsg(`Deploy-failed cleared (operator-attested) → ${result.serving_status ?? 'n/a'}`);
        onRefresh();
      } catch (forceError) {
        setActionMsg(forceError instanceof Error ? forceError.message : 'Request failed');
      }
    } finally {
      setActionLoading(null);
    }
  };

  return (
    <section data-testid="version-lifecycle-panel">
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Version Lifecycle</h2>
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 space-y-2 text-xs">
        <div className="flex flex-wrap items-center gap-2">
          {data.promotion_enabled !== undefined && (
            <>
              <div className="flex flex-col gap-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border ${optimisticPromotionEnabled ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400' : 'bg-neutral-500/10 border-neutral-500/30 text-neutral-400'}`} title="Fleet-wide setting (all projects)">
                    {optimisticPromotionEnabled ? 'LIVE' : 'DRY-RUN'}
                  </span>
                  {projectId && userRole === 'admin' && (
                    // eslint-disable-next-line viewport/min-touch-target
                    <button
                      onClick={handleTogglePromotionEnabled}
                      disabled={actionLoading !== null}
                      className="text-[10px] px-1.5 py-0.5 rounded border border-neutral-500/40 text-neutral-400 hover:bg-neutral-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                      title="Changes promotion mode for all projects"
                    >
                      {actionLoading === 'promotion' ? '…' : optimisticPromotionEnabled ? 'Disable' : 'Enable'}
                    </button>
                  )}
                </div>
                <span className="text-[9px] text-neutral-600">Fleet-wide setting</span>
              </div>
              {data.promotion_enabled_effective !== undefined && (
                <div className="flex flex-col gap-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border ${data.promotion_enabled_effective ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400' : 'bg-neutral-500/10 border-neutral-500/30 text-neutral-400'}`} title="Effective promotion mode for THIS project">
                      {data.promotion_enabled_effective ? 'LIVE' : 'DRY-RUN'}
                    </span>
                    {projectId && userRole === 'admin' && (
                      <>
                        {/* eslint-disable-next-line viewport/min-touch-target */}
                        <button
                          onClick={() => handleSetProjectPromotion(true)}
                          disabled={actionLoading !== null || (data.promotion_enabled_source === 'project' && data.promotion_enabled_override === true)}
                          className="text-[10px] px-1.5 py-0.5 rounded border border-emerald-500/40 text-emerald-400 hover:bg-emerald-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                          title="Arm live promotion for this project only"
                        >
                          {actionLoading === 'project-promotion' ? '…' : 'Arm'}
                        </button>
                        {/* eslint-disable-next-line viewport/min-touch-target */}
                        <button
                          onClick={() => handleSetProjectPromotion(false)}
                          disabled={actionLoading !== null || (data.promotion_enabled_source === 'project' && data.promotion_enabled_override === false)}
                          className="text-[10px] px-1.5 py-0.5 rounded border border-neutral-500/40 text-neutral-400 hover:bg-neutral-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                          title="Hold live promotion for this project only"
                        >
                          Hold
                        </button>
                        {data.promotion_enabled_source === 'project' && (
                          // eslint-disable-next-line viewport/min-touch-target
                          <button
                            onClick={() => handleSetProjectPromotion(null)}
                            disabled={actionLoading !== null}
                            className="text-[10px] px-1.5 py-0.5 rounded border border-neutral-500/40 text-neutral-400 hover:bg-neutral-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                            title="Clear this project's override and follow the fleet default"
                          >
                            Use fleet default
                          </button>
                        )}
                      </>
                    )}
                  </div>
                  <span className="text-[9px] text-neutral-600">
                    This project · {data.promotion_enabled_source === 'project' ? 'project override' : 'fleet default'}
                  </span>
                </div>
              )}
            </>
          )}
        </div>
        {cv && (
          <div className="space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-bold text-neutral-200">v{cv.version_number}</span>
              <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border ${cv.phase === 'stabilize' ? 'bg-amber-500/10 border-amber-500/30 text-amber-400' : 'bg-blue-500/10 border-blue-500/30 text-blue-400'}`}>{cv.phase}</span>
              {cv.phase_changed_at && <span className="text-[10px] text-neutral-500">{timeAgo(cv.phase_changed_at)}</span>}
              <span className="text-[10px] text-neutral-500">{cv.merged_task_count} tasks merged</span>
              {cv.qualification_attempts > 0 && <span className="text-[10px] text-neutral-500">{cv.qualification_attempts} qual{cv.qualification_attempts !== 1 ? 's' : ''}</span>}
              {cv.active_candidate_sha && (
                <span
                  data-testid="active-candidate-sha"
                  className="text-[10px] text-neutral-500 font-mono"
                  title={`candidate under evaluation: ${cv.active_candidate_sha}`}
                >
                  candidate @{cv.active_candidate_sha.slice(0, 7)}
                </span>
              )}
            </div>
            {editingGoal && projectId ? (
              <div className="flex flex-col gap-1.5">
                <textarea
                  value={goalInput}
                  onChange={(e) => setGoalInput(e.target.value)}
                  placeholder="Enter version goal..."
                  maxLength={500}
                  disabled={actionLoading !== null}
                  className="text-[11px] bg-neutral-800 border border-neutral-700 text-neutral-300 rounded px-2 py-1 focus:outline-none focus:border-neutral-600 disabled:opacity-50 disabled:cursor-not-allowed resize-none"
                  rows={2}
                />
                <div className="flex gap-1">
                  {/* eslint-disable-next-line viewport/min-touch-target */}
                  <button
                    onClick={handleSetGoal}
                    disabled={actionLoading !== null}
                    className="text-[10px] px-2 py-0.5 rounded border border-emerald-500/40 text-emerald-400 hover:bg-emerald-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {actionLoading === 'goal' ? '…' : 'Save'}
                  </button>
                  {/* eslint-disable-next-line viewport/min-touch-target */}
                  <button
                    onClick={() => {
                      setEditingGoal(false);
                      setGoalInput(cv.goal || '');
                    }}
                    disabled={actionLoading !== null}
                    className="text-[10px] px-2 py-0.5 rounded border border-neutral-500/40 text-neutral-400 hover:bg-neutral-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            ) : cv.goal ? (
              <div className="flex flex-col gap-1">
                <p className="text-[11px] text-neutral-300" title={cv.goal}>
                  <span className="text-neutral-500">Goal: </span>{cv.goal}
                </p>
                {cv.goal_updated_at && (
                  <p className="text-[10px] text-neutral-600">
                    {cv.goal_editor_info && <span>{cv.goal_editor_info} </span>}
                    <span>{timeAgo(cv.goal_updated_at)}</span>
                  </p>
                )}
                {cv.live_current_focus && cv.live_current_focus !== cv.goal && (
                  <div className="flex flex-col gap-1 pt-1 border-t border-amber-500/20">
                    <p className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-400 border border-amber-500/20">
                      Goal has drifted from live focus
                    </p>
                    <p className="text-[10px] text-neutral-400" title={cv.live_current_focus}>
                      <span className="text-neutral-600">Live focus: </span>{cv.live_current_focus}
                    </p>
                    {projectId && (
                      // eslint-disable-next-line viewport/min-touch-target
                      <button
                        onClick={() => {
                          setGoalInput(cv.live_current_focus || '');
                          setEditingGoal(true);
                        }}
                        disabled={actionLoading !== null}
                        className="text-[10px] px-1.5 py-0.5 w-fit rounded border border-amber-500/40 text-amber-400 hover:bg-amber-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                      >
                        Resync to focus
                      </button>
                    )}
                  </div>
                )}
                {projectId && (
                  // eslint-disable-next-line viewport/min-touch-target
                  <button
                    onClick={() => setEditingGoal(true)}
                    disabled={actionLoading !== null}
                    className="text-[10px] px-1.5 py-0.5 w-fit rounded border border-neutral-500/40 text-neutral-400 hover:bg-neutral-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    Edit
                  </button>
                )}
              </div>
            ) : projectId ? (
              // eslint-disable-next-line viewport/min-touch-target
              <button
                onClick={() => setEditingGoal(true)}
                disabled={actionLoading !== null}
                className="text-[10px] px-1.5 py-0.5 rounded border border-neutral-500/40 text-neutral-400 hover:bg-neutral-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                Set goal
              </button>
            ) : null}
            {cv.phase_reasoning && (
              <p className="text-[10px] text-neutral-400 italic truncate" title={cv.phase_reasoning}>{cv.phase_reasoning}</p>
            )}
            {cv.qualification_state && (
              <div className="pl-2 border-l border-neutral-700/50 space-y-0.5">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border ${QUAL_STATE_STYLE[cv.qualification_state.state] ?? 'bg-neutral-800 text-neutral-400 border-neutral-700'}`}>
                    {cv.qualification_state.state.replace(/_/g, ' ')}
                  </span>
                  <ServingStatusBadge
                    servingStatus={cv.aggregate_serving_status ?? cv.serving_status ?? cv.qualification_state.serving_status}
                    servedSha={cv.served_sha}
                    servedAt={cv.served_at}
                    deployFailureReason={cv.aggregate_deploy_failure_reason ?? cv.deploy_failure_reason ?? cv.qualification_state.deploy_failure_reason}
                    deployConfirmDeadline={cv.deploy_confirm_deadline}
                  />
                </div>
                {cv.qualification_state.state === 'promotion_in_flight' && promotionFreshness && (
                  <p
                    data-testid="promotion-in-flight-freshness"
                    className={`text-[10px] ${promotionFreshness.toneClassName}`}
                  >
                    Promotion PR{cv.qualification_state.promotion_pr_number ? ` #${cv.qualification_state.promotion_pr_number}` : ''} open & auto-merging ({promotionFreshness.detailText})
                  </p>
                )}
                <p className="text-[10px] text-neutral-400">{cv.qualification_state.summary}</p>
                <ArtifactEvidenceList artifacts={cv.artifacts} />
              </div>
            )}
            {cv.last_qualification && (
              <div className="pl-2 border-l border-neutral-700/50 space-y-0.5">
                <div className="flex flex-wrap items-center gap-2">
                  <QualSummary q={cv.last_qualification} />
                  {cv.last_qualification.candidate_sha && (() => {
                    const matchedVerdict = verdicts.find(v => v.candidate_sha === cv.last_qualification?.candidate_sha);
                    return matchedVerdict?.terminal_status === 'promoted' ? (
                      <PostPromotionOutcomeBadge outcome={matchedVerdict.post_promotion_outcome} outcomeAt={matchedVerdict.post_promotion_outcome_at} />
                    ) : null;
                  })()}
                  {cv.last_qualification.candidate_sha && (
                    <span
                      className="text-[10px] text-neutral-500 font-mono"
                      title={cv.last_qualification.candidate_sha}
                    >
                      @{cv.last_qualification.candidate_sha.slice(0, 7)}
                    </span>
                  )}
                  {cv.last_qualification.finalized_at && (
                    <span className="text-[10px] text-neutral-600">as of {timeAgo(cv.last_qualification.finalized_at)}</span>
                  )}
                </div>
                {(cv.merged_since_verdict ?? 0) > 0 && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded border bg-amber-500/10 text-amber-400 border-amber-500/20">
                    {cv.merged_since_verdict} task{cv.merged_since_verdict === 1 ? '' : 's'} merged since this verdict — re-qualify for current state
                  </span>
                )}
                {(cv.last_qualification.sub_scores || cv.last_qualification.ci_resolution) && (
                  <div className="flex flex-wrap gap-x-2 gap-y-0.5 text-[10px] text-neutral-500">
                    {cv.last_qualification.sub_scores && (
                      <>
                        <span>audit {Math.round(cv.last_qualification.sub_scores.audit * 100)}%</span>
                        <span>erosion {Math.round(cv.last_qualification.sub_scores.test_erosion * 100)}%</span>
                        <span>smoke {Math.round(cv.last_qualification.sub_scores.smoketest * 100)}%</span>
                      </>
                    )}
                    {cv.last_qualification.ci_resolution && (
                      <CiBadge ci={cv.last_qualification.ci_resolution} />
                    )}
                  </div>
                )}
                {cv.last_qualification.blocker && (
                  <p className="text-[10px] text-red-400 truncate">{cv.last_qualification.blocker}</p>
                )}
                {(cv.last_qualification.audit_missing_reason || (cv.last_qualification.consecutive_infra_errors ?? 0) > 0 || (cv.last_qualification.status === 'infra_failure' && cv.last_qualification.error)) && (
                  <div className="flex flex-wrap gap-1.5">
                    {cv.last_qualification.audit_missing_reason && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded border bg-amber-500/10 text-amber-400 border-amber-500/20">
                        {AUDIT_MISSING_REASON_LABELS[cv.last_qualification.audit_missing_reason] ?? cv.last_qualification.audit_missing_reason}
                      </span>
                    )}
                    {(cv.last_qualification.consecutive_infra_errors ?? 0) > 0 && (
                      <span className={`text-[10px] px-1.5 py-0.5 rounded border ${(cv.last_qualification.consecutive_infra_errors ?? 0) >= 3 ? 'bg-red-500/10 text-red-400 border-red-500/20' : 'bg-orange-500/10 text-orange-400 border-orange-500/20'}`}>
                        {cv.last_qualification.consecutive_infra_errors} consecutive infra failures
                      </span>
                    )}
                    {cv.last_qualification.status === 'infra_failure' && cv.last_qualification.error && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded border bg-red-500/10 text-red-400 border-red-500/20">
                        {cv.last_qualification.error}
                      </span>
                    )}
                  </div>
                )}
                {cv.last_qualification.audit_reconciliation && (
                  <AuditReconciliationRow rec={cv.last_qualification.audit_reconciliation} />
                )}
                <DurableQualificationEvidenceRow q={cv.last_qualification} />
              </div>
            )}
            {auditEvidenceState === 'current' && cv.latest_audit && (
              <div className="pl-2 border-l border-neutral-700/50">
                <AuditFindingsList audit={cv.latest_audit} />
              </div>
            )}
            {auditEvidenceState === 'stale' && cv.stale_audit && (
              <div className="pl-2 border-l border-neutral-700/50">
                <HistoricalAuditContext audit={cv.stale_audit} activeCandidateSha={cv.active_candidate_sha} />
              </div>
            )}
            {projectId && (
              <div data-testid="phase-controls" className="flex flex-wrap items-center gap-2 pt-1">
                {cv.phase === 'build' ? (
                  <button
                    onClick={() => handleSetPhase('stabilize')}
                    disabled={actionLoading !== null || promotionInFlight}
                    title={promotionInFlight ? `A promotion PR is open and auto-merging${promotionLockContext} — wait for it to land or cancel it before changing the phase` : undefined}
                    className="text-[10px] px-2 py-0.5 min-h-[44px] sm:min-h-0 rounded border border-amber-500/40 text-amber-400 hover:bg-amber-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {actionLoading === 'phase' ? '…' : '→ stabilize'}
                  </button>
                ) : (
                  <button
                    onClick={() => handleSetPhase('build')}
                    disabled={actionLoading !== null || promotionInFlight}
                    title={promotionInFlight ? `A promotion PR is open and auto-merging${promotionLockContext} — wait for it to land or cancel it before changing the phase` : undefined}
                    className="text-[10px] px-2 py-0.5 min-h-[44px] sm:min-h-0 rounded border border-blue-500/40 text-blue-400 hover:bg-blue-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {actionLoading === 'phase' ? '…' : '→ build'}
                  </button>
                )}
                {cv.phase === 'stabilize' && (
                  <button
                    onClick={handleQualify}
                    disabled={actionLoading !== null || promotionInFlight}
                    title={promotionInFlight ? `A promotion PR is open and auto-merging${promotionLockContext} — wait for it to land or cancel it before re-qualifying` : undefined}
                    className="text-[10px] px-2 py-0.5 min-h-[44px] sm:min-h-0 rounded border border-emerald-500/40 text-emerald-400 hover:bg-emerald-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {actionLoading === 'qualify' ? '…' : 'Run Qualify'}
                  </button>
                )}
                {promotionInFlight && (
                  <span
                    data-testid="promotion-in-flight-note"
                    className={`text-[10px] italic ${promotionFreshness?.toneClassName ?? 'text-emerald-300'}`}
                  >
                    Promotion PR{cv.qualification_state?.promotion_pr_number ? ` #${cv.qualification_state.promotion_pr_number}` : ''} open & auto-merging{promotionLockContext} — qualify & phase locked until it lands or is canceled
                  </span>
                )}
                {cuttingVersion ? (
                  <div className="flex flex-col gap-1 w-full">
                    <CutConsequences
                      cv={cv}
                      staged={data.staged_directions ?? []}
                    />
                    <div className="flex flex-wrap items-center gap-1">
                      <input
                        type="text"
                        value={cutReason}
                        onChange={(e) => setCutReason(e.target.value)}
                        placeholder="Optional reason for cut..."
                        disabled={actionLoading !== null}
                        maxLength={200}
                        className="text-[10px] flex-1 bg-neutral-800 border border-neutral-700 text-neutral-300 rounded px-2 py-0.5 focus:outline-none focus:border-neutral-600 disabled:opacity-50 disabled:cursor-not-allowed"
                      />
                      {/* eslint-disable-next-line viewport/min-touch-target */}
                      <button
                        onClick={handleCutVersion}
                        disabled={actionLoading !== null}
                        className="text-[10px] px-2 py-0.5 rounded border border-red-500/40 text-red-400 hover:bg-red-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                      >
                        {actionLoading === 'cut' ? '…' : 'Cut'}
                      </button>
                      {/* eslint-disable-next-line viewport/min-touch-target */}
                      <button
                        onClick={() => {
                          setCuttingVersion(false);
                          setCutReason('');
                        }}
                        disabled={actionLoading !== null}
                        className="text-[10px] px-2 py-0.5 rounded border border-neutral-500/40 text-neutral-400 hover:bg-neutral-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                ) : (
                  <button
                    onClick={() => setCuttingVersion(true)}
                    disabled={actionLoading !== null}
                    className="text-[10px] px-2 py-0.5 min-h-[44px] sm:min-h-0 rounded border border-red-500/40 text-red-400 hover:bg-red-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    Cut/Abandon
                  </button>
                )}
                {isAdmin && hasDeployFailed && (
                  <button
                    data-testid="clear-deploy-failed"
                    onClick={handleClearDeployFailed}
                    disabled={actionLoading !== null}
                    title="Clear a stale post-promotion deploy-failed serving state once the deploy is verified or attested live"
                    className="text-[10px] px-2 py-0.5 min-h-[44px] sm:min-h-0 rounded border border-orange-500/40 text-orange-400 hover:bg-orange-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {actionLoading === 'clear-deploy-failed' ? '…' : 'Clear deploy-failed'}
                  </button>
                )}
                {actionMsg && (
                  <span className="text-[10px] text-neutral-400 italic">{actionMsg}</span>
                )}
              </div>
            )}
          </div>
        )}
        {readiness !== undefined && <PromotionReadinessSummary readiness={readiness} />}
        {data.challenged_vs_unchallenged && (
          <ChallengedVsUnchallengedSummary telemetry={data.challenged_vs_unchallenged} />
        )}
        {data.staged_directions && data.staged_directions.length > 0 && (
          <StagedDirectionsSummary staged={data.staged_directions} />
        )}
        {verdicts.length > 0 && (
          <div data-testid="verdict-history">
            <p className="text-[10px] text-neutral-500 uppercase tracking-wide mb-1">Verdict History</p>
            <div className="space-y-1">
              {verdicts.slice(0, 8).map(v => (
                <div key={v.id} className="space-y-0.5 pl-2 border-l-2 border-neutral-700/40">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px]">
                    <span className={`font-mono font-semibold ${v.would_promote ? 'text-emerald-400' : 'text-amber-400'}`}>{v.confidence != null ? `${Math.round(v.confidence * 100)}%` : '—'}</span>
                    <span className={QUAL_VERDICT_COLOR[v.terminal_status ?? ''] ?? 'text-neutral-400'}>{v.terminal_status?.replace(/_/g, '-') ?? '?'}</span>
                    {v.promotion_enabled && <span className="text-[9px] font-mono text-emerald-600">LIVE</span>}
                    {v.sub_scores && <span className="text-neutral-600 font-mono text-[9px]">a:{Math.round(v.sub_scores.audit * 100)} e:{Math.round(v.sub_scores.test_erosion * 100)} s:{Math.round(v.sub_scores.smoketest * 100)}</span>}
                    {v.sub_scores?.ci_resolution && <CiBadge ci={v.sub_scores.ci_resolution} />}
                    {v.blocker && <span className="text-red-400 truncate max-w-[120px]" title={v.blocker}>{v.blocker}</span>}
                    {v.terminal_status === 'promoted' && (
                      <PostPromotionOutcomeBadge outcome={v.post_promotion_outcome} outcomeAt={v.post_promotion_outcome_at} />
                    )}
                    <span className="text-neutral-700 font-mono">{v.candidate_sha.slice(0, 7)}</span>
                    {v.finalized_at && <span className="text-neutral-600">{timeAgo(v.finalized_at)}</span>}
                  </div>
                  <DurableQualificationEvidenceRow q={v} />
                </div>
              ))}
            </div>
          </div>
        )}
        {recent.length > 0 && (
          <div>
            <p className="text-[10px] text-neutral-500 uppercase tracking-wide mb-1">History</p>
            <div className="space-y-0.5">
              {recent.slice(0, 4).map(v => (
                <div key={v.version_number} className="space-y-0.5">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px]">
                    <span className="text-neutral-400 font-mono w-8">v{v.version_number}</span>
                    <span className="text-neutral-500">{v.status}</span>
                    <ServingStatusBadge
                      servingStatus={v.aggregate_serving_status ?? v.serving_status ?? v.qualification_state?.serving_status}
                      servedSha={v.served_sha}
                      servedAt={v.served_at}
                      deployFailureReason={v.aggregate_deploy_failure_reason ?? v.deploy_failure_reason ?? v.qualification_state?.deploy_failure_reason}
                      deployConfirmDeadline={v.deploy_confirm_deadline}
                    />
                    <QualSummary q={v.last_qualification} />
                    {v.status === 'abandoned' && (v.abandoned_by || v.abandon_reason || v.abandoned_at) && (
                      <span className="text-neutral-400">
                        cut by {v.abandoned_by ?? 'unknown'}
                        {v.abandon_reason ? `: ${v.abandon_reason}` : ''}
                        {v.abandoned_at ? ` · ${timeAgo(v.abandoned_at)}` : ''}
                      </span>
                    )}
                  </div>
                  <ArtifactEvidenceList artifacts={v.artifacts} />
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </section>
  );
}
