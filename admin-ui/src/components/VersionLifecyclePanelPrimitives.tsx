/* eslint-disable react-refresh/only-export-components */
import { useState, useEffect } from 'react';
import type { JSX } from 'react';
import { LineChart, Line, ResponsiveContainer } from 'recharts';
import { fetchVersionSummary } from '../api';
import type {
  AgentServingShasResponse,
  AuditFinding,
  AuditReconciliation,
  BlindMergeGrade,
  ChallengerRefutedFinding,
  CiResolution,
  CurrentVersion,
  DimensionState,
  GoalAlignment,
  LatestAudit,
  OutOfBandPromotion,
  QualificationVerdictRecord,
  ReleaseArtifactEvidence,
  RecentVersion,
  StagedDirection,
  VersionQualification,
  VersionSummaryHighlights,
  VersionSummaryResponse,
  PromotionPolicySource,
  PromotionReadiness,
  QualificationState,
  QualificationStateValue,
  ChallengedVsUnchallengedTelemetry,
} from '../api';
import { timeAgo, formatDurationSec, deployWindowStatus } from '../utils/time';
import { VersionSummaryPanel } from './VersionSummaryPanel';

export const QUAL_VERDICT_COLOR: Record<string, string> = {
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
export function PostPromotionOutcomeBadge({
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
        className="font-mono text-ui-xs px-1 py-0.5 rounded border bg-emerald-500/10 border-emerald-500/30 text-emerald-400"
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
        className="font-mono text-ui-xs px-1 py-0.5 rounded border bg-red-500/10 border-red-500/40 text-red-400"
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
      className="font-mono text-ui-xs px-1 py-0.5 rounded border bg-amber-500/10 border-amber-500/30 text-amber-400"
      title="post-promotion outcome not yet recorded"
    >
      awaiting outcome
    </span>
  );
}

export function OutOfBandPromotionBadge({ oob }: { oob: OutOfBandPromotion }) {
  return (
    <span
      data-testid="out-of-band-promotion-badge"
      className="inline-flex items-center gap-1 text-ui-sm font-semibold px-1.5 py-0.5 rounded border bg-violet-500/10 border-violet-500/30 text-violet-400"
      title={`Promoted out-of-band (operator override) recorded ${oob.recorded_at}`}
    >
      ⚑ promoted out-of-band (operator override)
      <span className="font-mono text-violet-500/90">{oob.candidate_sha.slice(0, 7)}→{oob.promoted_sha.slice(0, 7)}</span>
      <span className="text-violet-600/90">{timeAgo(oob.recorded_at)}</span>
    </span>
  );
}

const POLICY_SOURCE_STYLE: Record<PromotionPolicySource, string> = {
  runtime_config: 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400',
  yaml_file: 'bg-sky-500/10 border-sky-500/30 text-sky-400',
  hardcoded_defaults: 'bg-amber-500/10 border-amber-500/30 text-amber-400',
};

export function PromotionPolicyBadge({
  source,
  error,
}: {
  source?: PromotionPolicySource | null;
  error?: string | null;
}) {
  if (!source) return null;
  const style = POLICY_SOURCE_STYLE[source] ?? 'bg-neutral-800 text-neutral-400 border-neutral-700';
  return (
    <div className="flex flex-wrap items-start gap-x-2 gap-y-0.5 text-ui-sm">
      <span className="text-neutral-500 uppercase tracking-wide">Policy source</span>
      <span
        data-testid="promotion-policy-source"
        className={`px-1.5 py-0.5 rounded border font-semibold ${style}`}
        title={`Promotion policy loaded from ${source}`}
      >
        {source}
      </span>
      {error && (
        <span
          data-testid="promotion-policy-source-error"
          className="text-amber-400"
          title={error}
        >
          fallback: {error}
        </span>
      )}
    </div>
  );
}

export function QualSummary({ q }: { q: VersionQualification | null }) {
  if (!q) return <span className="text-neutral-500 text-ui-sm">—</span>;
  if (q.status === 'pending' || q.status === 'qualifying') {
    return (
      <span className="text-blue-400 text-ui-sm" data-testid="qualification-progress">
        qualification in progress
      </span>
    );
  }
  if (q.confidence == null) return <span className="text-neutral-500 text-ui-sm">—</span>;
  const conf = `${Math.round(q.confidence * 100)}%`;
  const thr = q.threshold != null ? `/${Math.round(q.threshold * 100)}%` : '';
  const status = q.status ?? 'unknown';
  return (
    <span className={`font-mono text-ui-sm ${QUAL_VERDICT_COLOR[status] ?? 'text-neutral-400'}`}>
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

export function CiBadge({ ci }: { ci: CiResolution }) {
  const color = CI_STATE_STYLE[ci.state] ?? 'text-neutral-500';
  return (
    <span className={`font-mono text-ui-xs ${color}`} title={ci.detail}>
      ci:{ci.state}{ci.resolved_sha ? ` @${ci.resolved_sha.slice(0, 7)}` : ''}
    </span>
  );
}

export function ManagedCheckWaitSnapshotDetails({
  state,
}: {
  state: QualificationState | null | undefined;
}) {
  const snapshot = state?.check_snapshot;
  if (!snapshot) return null;
  const color = CI_STATE_STYLE[snapshot.state] ?? 'text-neutral-400';
  const checks = Array.isArray(snapshot.checks) ? snapshot.checks : [];
  const totalChecks =
    typeof snapshot.total_checks === 'number' && Number.isFinite(snapshot.total_checks)
      ? snapshot.total_checks
      : checks.length;
  return (
    <div
      data-testid="promotion-check-wait-snapshot"
      className="text-ui-sm text-neutral-400 border border-neutral-800 bg-neutral-950/40 rounded px-2 py-1 space-y-1"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className={`font-mono ${color}`}>ci:{snapshot.state}</span>
        <span title={snapshot.classified_at}>classified {timeAgo(snapshot.classified_at)}</span>
        {snapshot.head_sha && (
          <span className="font-mono text-neutral-500">head {snapshot.head_sha.slice(0, 7)}</span>
        )}
        <span>{totalChecks} check{totalChecks === 1 ? '' : 's'}</span>
        {snapshot.reason && <span className="text-amber-400">{snapshot.reason.replace(/_/g, ' ')}</span>}
      </div>
      {snapshot.detail && <p className="text-neutral-500">{snapshot.detail}</p>}
      {checks.length > 0 ? (
        <div className="flex flex-wrap gap-1">
          {checks.map((check, index) => {
            const status = check.conclusion ?? check.status ?? 'unknown';
            return (
              <span
                key={`${check.name}-${index}`}
                className="font-mono text-ui-xs px-1 py-0.5 rounded border border-neutral-800 bg-neutral-900 text-neutral-300"
                title={`${check.name}: ${status}`}
              >
                {check.name}: {status}
              </span>
            );
          })}
          {snapshot.truncated && (
            <span className="font-mono text-ui-xs px-1 py-0.5 rounded border border-neutral-800 text-neutral-500">
              +{Math.max(0, totalChecks - checks.length)} more
            </span>
          )}
        </div>
      ) : (
        <p className="text-neutral-500">No check runs reported yet.</p>
      )}
    </div>
  );
}

export const AUDIT_MISSING_REASON_LABELS: Record<string, string> = {
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

// Keyed by the authoritative QualificationStateValue union: a new backend
// qualification state without a style entry here is now a compile-time error
// (missing key), and a typo'd key fails the same way — the type is the pin.
export const QUAL_STATE_STYLE: Record<QualificationStateValue, string> = {
  qualifying: 'bg-amber-500/10 border-amber-500/30 text-amber-400',
  // The audit child crashed (terminal-failed, no report) while the verdict is
  // still pending — loud red, NOT the amber 'qualifying' look. Keeps the top
  // badge in agreement with the audit-findings section's 'audit crashed' cue.
  audit_crashed: 'bg-red-500/10 border-red-500/30 text-red-400',
  // Blueprint owns the promotion while required checks settle, then
  // revalidates the exact target and merges only the checked head.
  promotion_check_wait: 'bg-blue-500/10 border-blue-500/30 text-blue-400',
  promotion_failed: 'bg-red-500/10 border-red-500/30 text-red-400',
  // Promotion PR's required status checks failed — loud red, like promotion_failed.
  required_check_failed: 'bg-red-500/10 border-red-500/30 text-red-400',
  // Green checks but branch protection refused the merge (a human action is
  // required) — orange to signal "needs a human", distinct from a check failure.
  protection_blocked_review: 'bg-orange-500/10 border-orange-500/30 text-orange-400',
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
  // Qualification failed to start/complete due to a setup or operational error
  // (no_credentials, sha_error, spawn_failed) — loud red, config/human action needed.
  error: 'bg-red-500/10 border-red-500/30 text-red-400',
  // Audit infra failed mid-execution, no conclusive verdict — amber/caution to
  // distinguish from a setup error; re-qualify is the path forward.
  infra_failure: 'bg-amber-500/10 border-amber-500/30 text-amber-400',
  // Recoverable rubric holds: rubric changed mid-flight or provenance unknown.
  // Amber/caution — NOT a hard failure; a fresh grade resolves either hold.
  rubric_changed: 'bg-amber-500/10 border-amber-500/30 text-amber-400',
  rubric_provenance_unknown: 'bg-amber-500/10 border-amber-500/30 text-amber-400',
};

type PromotionFreshnessContext = {
  detailText: string;
  toneClassName: string;
};

function finiteSeconds(value: number | null | undefined): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  return Math.max(0, value);
}

export function formatPromotionFreshnessContext(
  state: QualificationState | null | undefined,
): PromotionFreshnessContext | null {
  const ageSeconds = finiteSeconds(state?.promotion_age_seconds);
  if (ageSeconds == null) return null;
  const timeoutSeconds = finiteSeconds(state?.promotion_timeout_seconds);
  let detailText = `waiting ~${formatDurationSec(ageSeconds)}`;
  if (timeoutSeconds != null && timeoutSeconds > 0) {
    detailText += ` (timeout cap ${formatDurationSec(timeoutSeconds)})`;
  }
  if (state?.promotion_past_timeout) {
    return {
      detailText: `${detailText}, past timeout`,
      toneClassName: 'text-red-400',
    };
  }
  if (state?.promotion_nearing_timeout) {
    return {
      detailText: `${detailText}, nearing timeout`,
      toneClassName: 'text-amber-400',
    };
  }
  return {
    detailText,
    toneClassName: 'text-blue-300',
  };
}

// Distinct, operator-readable serving-confirmation badge for a promoted version.
// Green = confirmed live; amber = still awaiting deploy confirmation; red = a
// deploy that failed to stabilize (with the failure reason). Renders nothing
// when there is no serving signal (e.g. a not-yet-promoted version).
export function ServingStatusBadge({
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
        className="inline-flex items-center gap-1 text-ui-sm font-semibold px-1.5 py-0.5 rounded border bg-emerald-500/10 border-emerald-500/30 text-emerald-400"
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
        className="inline-flex items-center gap-1 text-ui-sm font-semibold px-1.5 py-0.5 rounded border bg-red-500/10 border-red-500/40 text-red-400"
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
        className="inline-flex items-center gap-1 text-ui-sm font-semibold px-1.5 py-0.5 rounded border bg-amber-500/10 border-amber-500/40 text-amber-400"
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
        className={`inline-flex items-center gap-1 text-ui-sm font-semibold px-1.5 py-0.5 rounded border ${
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
export function ArtifactEvidenceList({
  artifacts,
  agentServingShas,
}: {
  artifacts?: ReleaseArtifactEvidence[] | null;
  agentServingShas?: AgentServingShasResponse | null;
}) {
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
        const showFleetMatch =
          a.artifact_kind === 'agent_image' &&
          a.serving_status !== 'serving_confirmed' &&
          agentServingShas != null;
        const fleetMatchCount = showFleetMatch && agentServingShas
          ? agentServingShas.fleet.buckets.filter((b) => b.matches_active_candidate === true).reduce((sum, b) => sum + b.count, 0)
          : 0;
        return (
          <div
            key={a.artifact_kind}
            data-testid="artifact-evidence-row"
            data-artifact-kind={a.artifact_kind}
            data-artifact-status={a.serving_status ?? 'none'}
            className="flex flex-wrap items-center gap-1.5 text-ui-xs"
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
            {showFleetMatch && (
              <span
                data-testid="fleet-match-count"
                className={fleetMatchCount > 0 ? 'text-blue-400' : 'text-neutral-500'}
              >
                {fleetMatchCount} VM{fleetMatchCount !== 1 ? 's' : ''} on candidate
              </span>
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
export function AuditProvenance({
  audit,
}: {
  audit: LatestAudit;
}) {
  if (!audit.candidate_sha && !audit.created_at) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {audit.candidate_sha && (
        <span className="text-ui-sm text-neutral-500 font-mono" title={audit.candidate_sha}>
          @{audit.candidate_sha.slice(0, 7)}
        </span>
      )}
      {audit.created_at && (
        <span className="text-ui-sm text-neutral-600">ran {timeAgo(audit.created_at)}</span>
      )}
    </div>
  );
}

export function AuditFindingsList({ audit }: { audit: LatestAudit }) {
  if (!audit.report) {
    const { label, className } = AUDIT_NO_REPORT_DISPLAY[classifyAuditNoReport(audit.task_status)];
    return (
      <div className="space-y-0.5">
        <AuditProvenance audit={audit} />
        <span className={`text-ui-sm px-1.5 py-0.5 rounded border ${className}`}>
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
        <span className="text-ui-sm text-neutral-500 italic">audit: no findings</span>
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
      <p className="text-ui-sm text-neutral-500 uppercase tracking-wide">Audit Findings</p>
      <AuditProvenance audit={audit} />
      {sorted.map((f, i) => (
        <div key={i} className={`flex items-start gap-1.5 text-ui-sm px-1.5 py-0.5 rounded border ${FINDING_SEVERITY_STYLE[f.severity?.toLowerCase() ?? ''] ?? 'bg-neutral-800/50 text-neutral-400 border-neutral-700/50'}`}>
          <span className="font-semibold shrink-0">{f.severity ?? '?'}</span>
          <span className="min-w-0 break-words">{f.summary}</span>
        </div>
      ))}
    </div>
  );
}

// Stale-audit view: the newest scoped audit ran against an older candidate than
// the one now under evaluation. When the active candidate SHA is known, contrast
// it explicitly against the audited commit so an operator can see exactly which
// commit is waiting for a fresh audit and which one the stale findings describe.
export function HistoricalAuditContext({
  audit,
  activeCandidateSha,
}: {
  audit: LatestAudit;
  activeCandidateSha?: string | null;
}) {
  const auditedShort = audit.candidate_sha ? `@${audit.candidate_sha.slice(0, 7)}` : null;
  return (
    <div data-testid="historical-audit-context" className="space-y-0.5">
      <p className="text-ui-sm text-neutral-500 uppercase tracking-wide">Historical Audit Context</p>
      <span className="text-ui-sm px-1.5 py-0.5 rounded border bg-amber-500/10 text-amber-400 border-amber-500/20">
        no current audit for this candidate
      </span>
      {activeCandidateSha ? (
        <div className="flex flex-wrap items-center gap-1.5 text-ui-sm">
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

export function AuditReconciliationRow({ rec }: { rec: AuditReconciliation }) {
  const hasDivergence = rec.score_delta != null && rec.score_delta >= 0.1;
  return (
    <div className="flex flex-wrap gap-1.5 items-center pt-0.5">
      <span className="text-ui-sm text-neutral-600 shrink-0">reconciliation:</span>
      {rec.model_reported_score != null && (
        <span className="text-ui-sm px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-400 border border-neutral-700 font-mono">
          model {Math.round(rec.model_reported_score * 100)}%
        </span>
      )}
      {rec.findings_derived_score != null && (
        <span className="text-ui-sm px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-400 border border-neutral-700 font-mono">
          findings {Math.round(rec.findings_derived_score * 100)}%
        </span>
      )}
      {rec.score_delta != null && (
        <span className={`text-ui-sm px-1.5 py-0.5 rounded border font-mono ${hasDivergence ? 'bg-amber-950/40 text-amber-400 border-amber-900/50' : 'bg-neutral-800 text-neutral-500 border-neutral-700'}`}>
          δ {Math.round(rec.score_delta * 100)}%
        </span>
      )}
      {rec.excluded_findings_count > 0 && (
        <span className="text-ui-sm px-1.5 py-0.5 rounded bg-amber-950/40 text-amber-500 border border-amber-900/50">
          {rec.excluded_findings_count} finding{rec.excluded_findings_count !== 1 ? 's' : ''} excluded
        </span>
      )}
    </div>
  );
}

export type DurableQualificationEvidence = {
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
  goal_alignment?: GoalAlignment | null;
  blind_merge_grade?: BlindMergeGrade | null;
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

export function DurableQualificationEvidenceRow({ q }: { q: DurableQualificationEvidence }) {
  const findings = q.audit_findings ?? [];
  const excludedFindings = q.excluded_audit_findings ?? [];
  const chips: JSX.Element[] = [];

  if (q.migration_safety || q.migration_safety_detail) {
    chips.push(
      <span
        key="migration-safety"
        className="text-ui-sm px-1.5 py-0.5 rounded border bg-neutral-800 text-neutral-400 border-neutral-700"
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
        className="text-ui-sm px-1.5 py-0.5 rounded border bg-neutral-800 text-neutral-400 border-neutral-700"
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
        className="text-ui-sm px-1.5 py-0.5 rounded border bg-sky-500/10 text-sky-300 border-sky-500/20"
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
        className="text-ui-sm px-1.5 py-0.5 rounded border bg-neutral-800 text-neutral-500 border-neutral-700"
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
        className="text-ui-sm px-1.5 py-0.5 rounded border bg-violet-500/10 text-violet-300 border-violet-500/20"
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
        className="text-ui-sm px-1.5 py-0.5 rounded border bg-neutral-800 text-neutral-400 border-neutral-700 break-all"
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
        className="text-ui-sm px-1.5 py-0.5 rounded border bg-neutral-800 text-neutral-400 border-neutral-700 break-all"
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
        className="text-ui-sm px-1.5 py-0.5 rounded border bg-neutral-800 text-neutral-500 border-neutral-700"
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
        className="text-ui-sm px-1.5 py-0.5 rounded border bg-red-500/10 text-red-400 border-red-500/20"
        title={q.promotion_error_detail ?? 'promotion conflict'}
      >
        promotion conflict
      </span>,
    );
  } else if (q.promotion_error_detail) {
    chips.push(
      <span
        key="promotion-error-detail"
        className="text-ui-sm px-1.5 py-0.5 rounded border bg-red-500/10 text-red-400 border-red-500/20"
        title={q.promotion_error_detail}
      >
        {q.promotion_error_detail}
      </span>,
    );
  }

  if (q.goal_alignment && q.goal_alignment.state !== 'not_applicable') {
    const ga = q.goal_alignment;
    const isAligned = ga.state === 'aligned';
    const isShortfall = ga.state === 'shortfall';
    const colorClass = isAligned
      ? 'bg-emerald-500/10 text-emerald-300 border-emerald-500/20'
      : isShortfall
        ? 'bg-amber-500/10 text-amber-300 border-amber-500/20'
        : 'bg-neutral-800 text-neutral-400 border-neutral-700';
    const gradeLabel =
      ga.assessed_grade != null ? ` ${Math.round(ga.assessed_grade * 100)}%` : '';
    chips.push(
      <span
        key="goal-alignment"
        data-testid="goal-alignment-chip"
        className={`text-ui-sm px-1.5 py-0.5 rounded border ${colorClass}`}
        title={ga.biggest_gap ?? ga.state ?? undefined}
      >
        goal {ga.state ?? 'unknown'}{gradeLabel}
      </span>,
    );
  }

  if (q.blind_merge_grade) {
    const bmg = q.blind_merge_grade;
    const verdict = bmg.coverage_verdict;
    const isCovers = verdict === 'covers';
    const isMisses = verdict === 'misses';
    const colorClass = isCovers
      ? 'bg-emerald-500/10 text-emerald-300 border-emerald-500/20'
      : isMisses
        ? 'bg-red-500/10 text-red-400 border-red-500/20'
        : 'bg-amber-500/10 text-amber-300 border-amber-500/20';
    chips.push(
      <span
        key="blind-merge-grade"
        data-testid="blind-merge-grade-chip"
        className={`text-ui-sm px-1.5 py-0.5 rounded border ${colorClass}`}
        title={bmg.biggest_gap ?? verdict ?? undefined}
      >
        merge {verdict ?? 'unknown'}
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

export function PromotionReadinessSummary({ readiness }: { readiness: PromotionReadiness | null | undefined }) {
  if (!readiness) return null;
  const { ready, reason, verdicts_evaluated, would_promote_rate, consecutive_clean_passes, recurring_blockers, caveat, ground_truth_validated, validated_precision, resolved_outcome_sample, acceptance_caveat } = readiness;
  const hasBlockers = Object.keys(recurring_blockers ?? {}).length > 0;
  // Once promotion is live, this dry-run window is frozen pre-go-live history —
  // it no longer receives verdicts, so it is NOT current readiness. Label it as a
  // historical calibration window rather than presenting a stale 'ready' number.
  const isHistoricalCalibration = readiness.is_current_readiness === false;
  if (isHistoricalCalibration) {
    return (
      <div className="mt-1 pt-1 border-t border-neutral-700/50 space-y-0.5">
        <span className="text-ui-sm font-semibold px-1.5 py-0.5 rounded border bg-neutral-500/10 border-neutral-500/30 text-neutral-400" title={reason}>
          pre-go-live dry-run calibration (historical)
        </span>
        <p className="text-ui-sm text-neutral-500 line-clamp-2">{reason}</p>
        {verdicts_evaluated > 0 && (
          <p className="text-ui-xs text-neutral-600">
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
          className={`text-ui-sm font-semibold px-1.5 py-0.5 rounded border ${ready ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400' : 'bg-amber-500/10 border-amber-500/30 text-amber-400'}`}
          title={explanatoryCaveat}
          data-testid="ready-badge"
        >
          {ready ? 'self-approval streak clears thresholds' : 'not ready'}
        </span>
        {verdicts_evaluated > 0 && (
          <span className="text-ui-sm text-neutral-500">{verdicts_evaluated} dry-run{verdicts_evaluated !== 1 ? 's' : ''}</span>
        )}
        {verdicts_evaluated > 0 && (
          <span className="text-ui-sm text-neutral-500" title={explanatoryCaveat}>{Math.round(would_promote_rate * 100)}% self-approval rate</span>
        )}
        {hasValidatedPrecision && (
          <span
            className="text-ui-sm font-semibold px-1.5 py-0.5 rounded border bg-emerald-500/10 border-emerald-500/30 text-emerald-400"
            title={`validated qualifier precision from ${resolved_outcome_sample} resolved promotion outcomes`}
          >
            {Math.round(validated_precision * 100)}% validated precision ({resolved_outcome_sample} resolved)
          </span>
        )}
        {consecutive_clean_passes > 0 && (
          <span className="text-ui-sm text-neutral-500">{consecutive_clean_passes} clean streak</span>
        )}
      </div>
      {ready && (
        <p className="text-ui-xs text-neutral-600 italic">{explanatoryCaveat}</p>
      )}
      {acceptance_caveat && (
        <p
          data-testid="acceptance-caveat-on-badge"
          className="text-ui-xs text-amber-400 italic"
          title={acceptance_caveat}
        >
          ⚠ {acceptance_caveat}
        </p>
      )}
      {!ready && reason && (
        <p className="text-ui-sm text-neutral-400 line-clamp-2">{reason}</p>
      )}
      {hasBlockers && (
        <p className="text-ui-sm text-red-400 truncate">
          recurring: {Object.entries(recurring_blockers).map(([k, n]) => `${k}×${n}`).join(', ')}
        </p>
      )}
    </div>
  );
}

export function PinnedServingVersionCallout({ version }: { version: RecentVersion }) {
  const servingStatus = version.serving?.status ?? version.serving_status;
  if (servingStatus !== 'serving_confirmed') return null;
  const mergedCount = version.merged_count ?? version.merged_task_count ?? version.highlights?.merged_count ?? null;
  const promotedAt = version.promoted_at ?? version.serving?.confirmed_at ?? version.served_at;
  return (
    <div
      data-testid="pinned-serving-version"
      className="flex flex-col gap-0.5 px-2 py-1.5 rounded border bg-emerald-500/10 border-emerald-500/30"
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-ui-sm font-semibold text-emerald-400">✓ currently serving</span>
        <span className="text-ui-sm font-mono text-emerald-300">v{version.version_number}</span>
        {mergedCount != null && (
          <span className="text-ui-sm text-emerald-600">{mergedCount} merged</span>
        )}
        {promotedAt && (
          <span className="text-ui-sm text-emerald-700">promoted {timeAgo(promotedAt)}</span>
        )}
      </div>
      {version.highlights?.headline && (
        <p className="text-ui-sm text-emerald-300/80 italic">{version.highlights.headline}</p>
      )}
    </div>
  );
}

export function PromotedAwaitingActivationCallout({
  version,
  canApprove = false,
  approving = false,
  approvalError = null,
  onApprove,
  optimisticApproval = null,
}: {
  version: RecentVersion;
  canApprove?: boolean;
  approving?: boolean;
  approvalError?: string | null;
  onApprove?: () => void;
  optimisticApproval?: { activation_requested_at: string; activation_requested_by: string } | null;
}) {
  const servingStatus = version.serving?.status ?? version.serving_status;
  // Only the "promoted, still unconfirmed" state is awaiting the operator's
  // single activation approval — serving_confirmed and deploy_failed have their
  // own surfaces, and an un-armed promotion reads as NULL/NA, not pending.
  if (version.status !== 'promoted' || servingStatus !== 'unconfirmed') return null;
  const promotedAt = version.promoted_at ?? version.serving?.confirmed_at ?? version.served_at;
  const deadline = version.deploy_confirm_deadline;
  const deadlineText = deadline
    ? ` · confirm by ${new Date(deadline).toLocaleString()}`
    : '';
  const activationRequestedAt = version.activation_requested_at ?? optimisticApproval?.activation_requested_at;
  const activationRequestedBy = version.activation_requested_by ?? optimisticApproval?.activation_requested_by;
  const approved = !!activationRequestedAt;
  return (
    <div
      data-testid="promoted-awaiting-activation"
      className="flex flex-col gap-0.5 px-2 py-1.5 rounded border bg-amber-500/10 border-amber-500/30"
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-ui-sm font-semibold text-amber-400">
          {approved ? 'approved — awaiting activation' : 'promoted, awaiting activation'}
        </span>
        <span className="text-ui-sm font-mono text-amber-300">v{version.version_number}</span>
        {promotedAt && (
          <span className="text-ui-sm text-amber-600">promoted {timeAgo(promotedAt)}</span>
        )}
      </div>
      <p className="text-ui-sm text-amber-300/80">
        {approved
          ? <>Approved by {activationRequestedBy || 'an administrator'}; awaiting the activation receipt{deadlineText}.</>
          : <>Approve activation to have the host-side deploy agent perform the bound release, or run <span className="font-mono">blueprint deploy</span> for an emergency override{deadlineText}.</>}
      </p>
      {canApprove && !approved && onApprove && (
        <button
          type="button"
          data-testid="approve-activation"
          onClick={onApprove}
          disabled={approving}
          className="self-start text-ui-sm px-2 py-1 min-h-[44px] sm:min-h-0 rounded border border-amber-500/40 text-amber-300 hover:bg-amber-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {approving ? 'Approving…' : 'Approve activation'}
        </button>
      )}
      {approvalError && (
        <p role="alert" data-testid="approve-activation-error" className="text-ui-sm text-red-400">
          {approvalError}
        </p>
      )}
    </div>
  );
}

export function StagedDirectionsSummary({ staged }: { staged: StagedDirection[] }) {
  if (!staged.length) return null;
  return (
    <div data-testid="staged-directions">
      <p className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-1">
        Staged for next version ({staged.length})
      </p>
      <div className="space-y-0.5">
        {staged.map((s, i) => (
          <div key={s.id ?? i} className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-ui-sm pl-2 border-l-2 border-neutral-700/40">
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
export function stagedActivatedByCut(staged: StagedDirection[], versionNumber: number): StagedDirection[] {
  return staged.filter(
    (s) => s.staged_for_version == null || s.staged_for_version <= versionNumber,
  );
}

// Disclosure of a version cut's concrete effects, shown before the operator
// confirms: which staged directions begin running now and how much in-flight
// work keeps running against the freshly-opened version.
export function CutConsequences({ cv, staged }: { cv: CurrentVersion; staged: StagedDirection[] }) {
  const activating = stagedActivatedByCut(staged, cv.version_number);
  const inFlight = cv.in_flight_task_count;
  const qualState: QualificationState | null = cv.qualification_state ?? null;
  const promotionFreshness = formatPromotionFreshnessContext(qualState);
  return (
    <div
      data-testid="cut-consequences"
      className="text-ui-sm text-neutral-300 bg-amber-950/30 border border-amber-900/40 rounded px-2 py-1 space-y-1"
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
      {inFlight == null ? (
        <p className="text-neutral-500">In-flight task count unavailable.</p>
      ) : (
        <p className={inFlight > 0 ? 'text-neutral-300' : 'text-neutral-500'}>
          {inFlight > 0
            ? `${inFlight} in-flight task${inFlight !== 1 ? 's' : ''} will NOT be stopped — they keep running and land against the new version.`
            : 'No tasks are currently in flight.'}
        </p>
      )}
      {qualState?.state === 'promotion_check_wait' && (
        <p className={promotionFreshness?.toneClassName ?? 'text-blue-300'}>
          Promotion PR{qualState.promotion_pr_number ? ` #${qualState.promotion_pr_number}` : ''} is open in Blueprint&apos;s exact-head check wait
          {promotionFreshness ? ` (${promotionFreshness.detailText})` : ''} — Blueprint revalidates the PR target and merges only the checked head after required checks pass; this cut will be rejected until it lands or is canceled.
        </p>
      )}
    </div>
  );
}

export function ChallengedVsUnchallengedSummary({ telemetry }: { telemetry: ChallengedVsUnchallengedTelemetry }) {
  const { challenged, unchallenged, total_verdicts } = telemetry;
  const pct = (r: number) => `${Math.round(r * 100)}%`;
  const bothInsufficient = challenged.insufficient_sample && unchallenged.insufficient_sample;
  return (
    <div data-testid="challenged-vs-unchallenged" className="mt-1 pt-1 border-t border-neutral-700/50 space-y-0.5">
      <p className="text-ui-sm text-neutral-500 uppercase tracking-wide">Audit Challenge Impact ({total_verdicts} verdicts)</p>
      {bothInsufficient ? (
        <span className="text-ui-sm text-neutral-600 italic">not enough data yet (need ≥3 per cohort)</span>
      ) : (
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          {([{ label: 'challenged', s: challenged }, { label: 'unchallenged', s: unchallenged }] as const).map(({ label, s }) => (
            <div key={label} className="space-y-0.5">
              <span className="text-ui-sm text-neutral-500">{label} ({s.sample_size})</span>
              {s.insufficient_sample ? (
                <span className="text-ui-sm text-neutral-600 italic"> — insufficient</span>
              ) : (
                <div className="flex flex-wrap gap-1">
                  <span className="text-ui-sm px-1 py-0 rounded bg-neutral-800 border border-neutral-700 text-neutral-400" title="exclusion rate — fraction of verdicts with audit findings excluded for lacking verification">excl {pct(s.exclusion_rate ?? 0)}</span>
                  {s.disagreement_rate != null && (
                    <span className={`text-ui-sm px-1 py-0 rounded border ${(s.disagreement_rate ?? 0) > 0.2 ? 'bg-amber-500/10 border-amber-500/30 text-amber-400' : 'bg-neutral-800 border-neutral-700 text-neutral-400'}`} title="challenger disagreement rate — fraction where the challenger did not fully agree">disagree {pct(s.disagreement_rate)}</span>
                  )}
                  {s.wrong_decision_rate != null && (
                    <span className={`text-ui-sm px-1 py-0 rounded border ${s.wrong_decision_rate > 0 ? 'bg-red-500/10 border-red-500/30 text-red-400' : 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400'}`} title={`post-promotion wrong decision rate over ${s.wrong_decision_resolved_sample ?? 0} resolved promotions`}>wrong {pct(s.wrong_decision_rate)}</span>
                  )}
                  {s.challenge_refuted_rate != null && (
                    <span className={`text-ui-sm px-1 py-0 rounded border ${(s.challenge_refuted_rate ?? 0) > 0 ? 'bg-violet-500/10 border-violet-500/30 text-violet-400' : 'bg-neutral-800 border-neutral-700 text-neutral-400'}`} title={`challenger-refuted false positive rate — fraction of verdicts where the adversarial challenger explicitly refuted findings as false positives (${s.challenge_refuted_count ?? 0} total refuted findings)`}>refuted {pct(s.challenge_refuted_rate)}</span>
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

/** Shared italic what's-new headline + at-a-glance metric line (merged / proven /
 *  conf-thr / phase·status). Rendered for both the current (open) version and the
 *  expanded past versions so they present an identical summary. The headline string
 *  itself is computed server-side (release_version.build_version_highlights) and its
 *  'serving' word reflects the aggregate required-artifact serving truth. */
export function VersionHighlightsLine({
  highlights,
  phase,
  status,
  qualificationState,
}: {
  highlights: VersionSummaryHighlights;
  phase?: string | null;
  status: string;
  qualificationState?: QualificationStateValue | null;
}) {
  const qualificationInProgress = qualificationState === 'qualifying';
  const conf =
    !qualificationInProgress && highlights.qualification_confidence != null
      ? `${Math.round(highlights.qualification_confidence * 100)}%`
      : null;
  const thresh =
    highlights.qualification_threshold != null
      ? `${Math.round(highlights.qualification_threshold * 100)}%`
      : null;
  return (
    <div className="space-y-0.5 text-ui-sm" data-testid="version-headline">
      <p className="text-neutral-300 italic">{highlights.headline}</p>
      <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-neutral-400">
        <span>{highlights.merged_count} merged</span>
        {highlights.total_merged_for_acceptance > 0 ? (
          <span>{highlights.proven}/{highlights.total_merged_for_acceptance} merged-task proofs</span>
        ) : (
          <span data-testid="no-eligible-merged-task-proofs">no eligible merged-task proofs</span>
        )}
        {qualificationInProgress && (
          <span className="text-blue-400" data-testid="highlights-qualification-progress">
            qualification in progress
          </span>
        )}
        {conf && thresh && <span>conf {conf} / thr {thresh}</span>}
        {phase && <span className="text-neutral-500">{phase} · {status}</span>}
      </div>
      {highlights.narrative && (
        <p className="text-neutral-400 mt-1 leading-relaxed" data-testid="version-narrative">{highlights.narrative}</p>
      )}
    </div>
  );
}

export function VersionHistorySummary({ versionId }: { versionId: string }) {
  const [summary, setSummary] = useState<VersionSummaryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  // loading is derived: no data and no error yet
  const loading = summary === null && error === null;

  useEffect(() => {
    let cancelled = false;
    fetchVersionSummary(versionId)
      .then(data => { if (!cancelled) setSummary(data); })
      .catch(err => { if (!cancelled) setError(String(err)); });
    return () => { cancelled = true; };
  }, [versionId]);

  if (loading) return <p className="text-ui-sm text-neutral-500 pl-2">Loading…</p>;
  if (error || !summary) return <p className="text-ui-sm text-red-400 pl-2">Failed to load summary</p>;

  return (
    <div className="mt-1 pl-2 border-l-2 border-neutral-700/40 space-y-0.5 text-ui-sm">
      <VersionHighlightsLine
        highlights={summary.highlights}
        phase={summary.phase}
        status={summary.status}
        qualificationState={summary.qualification_state?.state}
      />
      <VersionSummaryPanel tasksMerged={summary.tasks_merged} acceptanceAggregate={summary.acceptance_aggregate} />
    </div>
  );
}

export function VersionLifecycleRefreshBanner({
  kind,
  message,
  lastSuccessfulRefreshAt,
  refreshing,
  canRetry,
  onRefresh,
}: {
  kind: 'stale' | 'unavailable';
  message: string;
  lastSuccessfulRefreshAt?: string | null;
  refreshing: boolean;
  canRetry: boolean;
  onRefresh: () => void;
}) {
  const stale = kind === 'stale';
  const toneClass = stale
    ? 'mb-2 border-amber-500/30 bg-amber-500/10 text-amber-200'
    : 'border-red-500/30 bg-red-500/10 text-red-300';
  const detailClass = stale ? 'text-amber-200/90' : 'text-red-300/90';
  const buttonClass = stale
    ? 'border-amber-400/40 text-amber-100 hover:bg-amber-500/10'
    : 'border-red-400/40 text-red-200 hover:bg-red-500/10';
  return (
    <div role="alert" data-testid={stale ? 'version-lifecycle-stale' : 'version-lifecycle-unavailable'} className={`${toneClass} rounded-lg border px-3 py-2 text-xs`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className={`font-semibold ${stale ? '' : 'text-red-200'}`}>
            {stale ? 'Version lifecycle data may be stale' : 'Version lifecycle unavailable'}
          </p>
          <p className={`text-ui-md ${detailClass}`}>
            {message}{stale && lastSuccessfulRefreshAt ? `; last successful refresh ${timeAgo(lastSuccessfulRefreshAt)}` : ''}
          </p>
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={refreshing || !canRetry}
          className={`min-h-[44px] rounded border px-2 py-1 text-ui-md disabled:cursor-not-allowed disabled:opacity-50 ${buttonClass}`}
        >
          {refreshing ? 'Retrying...' : 'Retry'}
        </button>
      </div>
    </div>
  );
}

const DIMENSION_STATUS_DOT: Record<string, string> = {
  passed: 'bg-emerald-500',
  failed: 'bg-red-500',
  unavailable: 'bg-amber-400',
};

/** Per-dimension breakdown of qualification state: colored status dot, label,
 *  optional score, and reason text for failed/unavailable rows. */
export function DimensionBreakdown({ dimensions }: { dimensions: DimensionState[] }) {
  if (dimensions.length === 0) return null;
  return (
    <div data-testid="dimension-states" className="space-y-0.5 py-0.5">
      {dimensions.map(d => {
        const dotClass = DIMENSION_STATUS_DOT[d.status] ?? 'bg-neutral-500';
        const isFailed = d.status === 'failed';
        const isUnavailable = d.status === 'unavailable';
        const isPassed = d.status === 'passed';
        const rowBg = isFailed
          ? 'bg-red-500/5 border border-red-500/20 rounded px-1.5'
          : '';
        return (
          <div
            key={d.key}
            className={`flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-ui-sm ${rowBg}`}
            data-dimension={d.key}
            data-status={d.status}
          >
            <span
              className={`inline-block w-2 h-2 rounded-full shrink-0 mt-0.5 ${dotClass}`}
              aria-label={d.status}
            />
            <span className={isPassed ? 'text-neutral-500' : isFailed ? 'text-red-300 font-medium' : isUnavailable ? 'text-amber-300' : 'text-neutral-500'}>
              {d.label}
            </span>
            {d.score != null && (
              <span className="font-mono text-neutral-500">
                {Math.round(d.score * 100)}%
              </span>
            )}
            {d.reason && (
              <span
                className={`truncate max-w-[200px] ${isFailed ? 'text-red-400' : 'text-amber-400'}`}
                title={d.reason}
              >
                {d.reason}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function ConfidenceSparkline({ verdicts }: { verdicts: QualificationVerdictRecord[] }) {
  const points = verdicts
    .slice()
    .reverse()
    .filter(v => v.confidence != null)
    .map((v, i) => ({ i, v: Math.round((v.confidence as number) * 100), promote: v.would_promote }));

  if (points.length < 2) return null;

  const promotedCount = points.filter(p => p.promote).length;
  const color = promotedCount >= points.length / 2 ? '#10b981' : '#f59e0b';

  return (
    <div
      data-testid="confidence-sparkline"
      style={{ height: 28 }}
      title={`Confidence trend over last ${points.length} verdicts (oldest → newest)`}
    >
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={points} margin={{ top: 2, right: 0, left: 0, bottom: 2 }}>
          <Line dataKey="v" stroke={color} dot={false} strokeWidth={1.5} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
