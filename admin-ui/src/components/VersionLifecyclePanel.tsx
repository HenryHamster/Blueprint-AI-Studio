/* eslint-disable react-hooks/set-state-in-effect */
import { useState, useEffect, useRef } from 'react';
import { ConfirmDialog } from './ConfirmDialog';
import { VersionSummaryPanel } from './VersionSummaryPanel';
import {
  attestAcceptanceCriterion,
  clearDeployFailed,
  cutVersion,
  fetchQualificationVerdicts,
  getHotfixPreview,
  linkCriteriaToDirection,
  patchAdminSettings,
  reinstateAcceptanceCriterion,
  setProjectPromotionEnabled,
  setVersionPhase,
  setVersionGoal,
  triggerHotfixPromote,
  triggerQualify,
  type AgentServingShasResponse,
  type HotfixPreviewResponse,
  type QualificationVerdictRecord,
  type CurrentVersion,
  type VersionLifecycleResponse,
  type RecentVersion,
  type QualificationState,
  type QualificationTriggerState,
} from '../api';
import { timeAgo } from '../utils/time';
import {
  QUAL_VERDICT_COLOR,
  QUAL_STATE_STYLE,
  AUDIT_MISSING_REASON_LABELS,
  formatPromotionFreshnessContext,
  stagedActivatedByCut,
  PostPromotionOutcomeBadge,
  OutOfBandPromotionBadge,
  PromotionPolicyBadge,
  QualSummary,
  CiBadge,
  ManagedCheckWaitSnapshotDetails,
  AuditFindingsList,
  HistoricalAuditContext,
  AuditReconciliationRow,
  DurableQualificationEvidenceRow,
  PromotionReadinessSummary,
  PinnedServingVersionCallout,
  StagedDirectionsSummary,
  CutConsequences,
  ChallengedVsUnchallengedSummary,
  VersionHighlightsLine,
  VersionHistorySummary,
  VersionLifecycleRefreshBanner,
  ServingStatusBadge,
  ArtifactEvidenceList,
  ConfidenceSparkline,
  DimensionBreakdown,
} from './VersionLifecyclePanelPrimitives';

const TRIGGER_AUTO_LABELS: Record<string, { label: string; cls: string }> = {
  armed:     { label: 'armed',     cls: 'text-emerald-400' },
  waiting:   { label: 'waiting',   cls: 'text-amber-400' },
  in_flight: { label: 'in flight', cls: 'text-sky-400' },
  capped:    { label: 'capped',    cls: 'text-red-400' },
  settled:   { label: 'settled',   cls: 'text-neutral-500' },
  observe:   { label: 'observe',   cls: 'text-neutral-500' },
  off:       { label: 'off',       cls: 'text-neutral-600' },
};

function QualificationTriggerRow({ state }: { state: QualificationTriggerState }) {
  const { label, cls } = TRIGGER_AUTO_LABELS[state.auto_trigger] ?? { label: state.auto_trigger, cls: 'text-neutral-400' };
  return (
    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-ui-sm" data-testid="qualification-trigger-state">
      <span className="text-neutral-500">auto-trigger</span>
      <span className={`font-medium ${cls}`}>{label}</span>
      {state.blocking.length > 0 && (
        <span className="text-neutral-600">— {state.blocking[0]}</span>
      )}
      {state.next_expected_trigger && state.auto_trigger === 'waiting' && (
        <span className="text-neutral-600 italic">next: {state.next_expected_trigger.replace(/_/g, ' ')}</span>
      )}
      <span className="text-neutral-600">
        lifetime qualifications: {state.lifetime_qualification_attempts}
      </span>
      <span className="text-neutral-600">
        error retry episode: {state.error_retry_episode.count}/{state.error_retry_episode.cap}
      </span>
    </div>
  );
}

// Generate a client idempotency key for a version cut. Prefers the native UUID
// generator and falls back to a timestamp+random token for older runtimes (and
// JSDOM, where crypto.randomUUID may be absent). Mirrors MessagesPanel.
function generateIdempotencyKey(): string {
  const c = typeof globalThis !== 'undefined' ? globalThis.crypto : undefined;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  return `idem-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function DebtEnforcePreview({
  q,
}: {
  q: {
    debt_enforce_confidence_preview?: number | null;
    debt_enforce_weight_preview?: number | null;
  } | null;
}) {
  if (
    !q ||
    q.debt_enforce_confidence_preview == null ||
    q.debt_enforce_weight_preview == null
  ) {
    return null;
  }
  return (
    <span
      data-testid="debt-enforce-preview"
      className="text-ui-xs font-mono text-cyan-400"
      title="Observe-mode confidence preview if completion-debt weight mode were enforced"
    >
      enforce {Math.round(q.debt_enforce_confidence_preview * 100)}%
      {` @w${q.debt_enforce_weight_preview.toFixed(2)}`}
    </span>
  );
}

interface VersionLifecyclePanelProps {
  data: VersionLifecycleResponse | null;
  loading: boolean;
  projectId: string | null;
  onRefresh: () => void;
  userRole?: 'admin' | 'user' | null;
  refreshError?: string | null;
  lastSuccessfulRefreshAt?: string | null;
  refreshing?: boolean;
  agentServingShas?: AgentServingShasResponse | null;
}

interface LocalDialogConfig {
  title: string;
  message: string;
  confirmLabel?: string;
  attestationInput?: { label: string; placeholder?: string };
  onConfirm: (reason?: string) => void;
}

export function VersionLifecyclePanel({ data, loading, projectId, onRefresh, userRole, refreshError, lastSuccessfulRefreshAt, refreshing = false, agentServingShas }: VersionLifecyclePanelProps) {
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [localDialog, setLocalDialog] = useState<LocalDialogConfig | null>(null);
  const [verdicts, setVerdicts] = useState<QualificationVerdictRecord[]>([]);
  const [verdictsError, setVerdictsError] = useState<string | null>(null);
  const [editingGoal, setEditingGoal] = useState<boolean>(false);
  const [goalInput, setGoalInput] = useState<string>('');
  const [optimisticOverrides, setOptimisticOverrides] = useState<Partial<CurrentVersion> | null>(null);
  const [promotionEnabledOverride, setPromotionEnabledOverride] = useState<boolean | null>(null);
  const [cuttingVersion, setCuttingVersion] = useState<boolean>(false);
  const [cutReason, setCutReason] = useState<string>('');
  const [hotfixPanelOpen, setHotfixPanelOpen] = useState<boolean>(false);
  const [hotfixPreview, setHotfixPreview] = useState<HotfixPreviewResponse | null>(null);
  const [hotfixPreviewLoading, setHotfixPreviewLoading] = useState<boolean>(false);
  const [hotfixReason, setHotfixReason] = useState<string>('');
  const [hotfixAck, setHotfixAck] = useState<boolean>(false);
  const [expandedVersionId, setExpandedVersionId] = useState<string | null>(null);
  const [changelogExpanded, setChangelogExpanded] = useState<boolean>(false);
  // One idempotency key per confirmed cut: generated on confirm, reused across
  // a lost-response retry, cleared on success so the next cut gets a fresh key.
  const cutIdempotencyKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (!projectId) { setVerdicts([]); setVerdictsError(null); return; }
    fetchQualificationVerdicts(projectId, 10)
      .then(r => { setVerdicts(r.verdicts); setVerdictsError(null); })
      .catch(() => { setVerdictsError('Failed to load verdict history.'); });
  }, [projectId, data]);
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

  if (loading) return null;
  if (!data || (!data.current_version && data.recent_versions.length === 0)) {
    if (!refreshError) return null;
    return (
      <section data-testid="version-lifecycle-panel">
        <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">Version Lifecycle</h2>
        <VersionLifecycleRefreshBanner kind="unavailable" message={refreshError} refreshing={refreshing} canRetry={!!projectId} onRefresh={onRefresh} />
      </section>
    );
  }
  const cv = data.current_version ? (optimisticOverrides ? { ...data.current_version, ...optimisticOverrides } : data.current_version) : null;
  const recent = data.recent_versions;
  const promotionEnabled = data.promotion_enabled ?? false;
  const optimisticPromotionEnabled = promotionEnabledOverride ?? promotionEnabled;
  const readiness = data.promotion_readiness;
  const policySource = data.promotion_policy_source ?? null;
  const policyError = data.promotion_policy_error ?? null;
  // Blueprint keeps authority over every pending promotion PR while required
  // checks settle, then revalidates the exact base/head target before merging.
  // Re-qualifying or flipping phase would clobber that durable wait record, so
  // the backend rejects those actions and the UI locks them pre-emptively.
  const promotionCheckWait = cv?.qualification_state?.state === 'promotion_check_wait';
  const promotionLocked = promotionCheckWait;
  const promotionFreshness = formatPromotionFreshnessContext(cv?.qualification_state);
  const promotionLockContext = promotionFreshness ? ` (${promotionFreshness.detailText})` : '';
  const phaseLockTitle = promotionCheckWait
    ? `A promotion PR is open in Blueprint's exact-head check wait${promotionLockContext} — Blueprint will merge only the revalidated checked head; wait for it to land or cancel it before changing the phase`
    : undefined;
  const qualifyLockTitle = promotionCheckWait
    ? `A promotion PR is open in Blueprint's exact-head check wait${promotionLockContext} — Blueprint will merge only the revalidated checked head; wait for it to land or cancel it before re-qualifying`
    : undefined;
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
  const hasDeployFailed = (cv?.serving?.status ?? cv?.serving_status) === 'deploy_failed'
    || recent.some(v => (v.serving?.status ?? v.serving_status) === 'deploy_failed');

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

  const handleTogglePromotionEnabled = () => {
    const nextValue = !optimisticPromotionEnabled;
    const doAction = async () => {
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
    if (nextValue) {
      setLocalDialog({
        title: 'Enable fleet-wide live promotion?',
        message: 'Enable live promotion for ALL PROJECTS? Passing qualify runs will push to main fleet-wide.',
        confirmLabel: 'Enable',
        onConfirm: () => { setLocalDialog(null); doAction(); },
      });
    } else {
      doAction();
    }
  };

  const handleSetProjectPromotion = (enabled: boolean | null) => {
    if (!projectId) return;
    const doAction = async () => {
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
    if (enabled === true) {
      setLocalDialog({
        title: 'Arm project live promotion?',
        message: 'Arm live promotion for THIS project only? Passing qualify runs will push this project to main.',
        confirmLabel: 'Arm',
        onConfirm: () => { setLocalDialog(null); doAction(); },
      });
    } else {
      doAction();
    }
  };

  const handleCutVersion = () => {
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
      qualState?.state === 'promotion_check_wait'
        ? `promotion PR${qualState.promotion_pr_number ? ` #${qualState.promotion_pr_number}` : ''} is open in Blueprint's exact-head check wait${cutPromotionFreshness ? ` (${cutPromotionFreshness.detailText})` : ''} (only the revalidated checked head can merge)`
        : null,
    ].filter(Boolean);
    const msg = consequences.length
      ? `Abandon v${cv.version_number} and open a fresh version?\n\nThis will: ${consequences.join('; ')}.`
      : `Abandon v${cv.version_number} and open a fresh version?`;
    setLocalDialog({
      title: `Abandon v${cv.version_number}?`,
      message: msg,
      confirmLabel: 'Abandon',
      onConfirm: async () => {
        setLocalDialog(null);
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
      },
    });
  };

  // Clear a stale post-promotion deploy_failed serving state. First attempts the
  // verify path (no force) — the backend clears only if the promoted SHA is
  // independently observed serving. If it refuses (live SHA unobservable), show
  // an attestation-reason dialog and retry the operator-attested force path.
  const handleClearDeployFailed = () => {
    if (!projectId) return;
    setLocalDialog({
      title: 'Clear deploy-failed?',
      message: 'Clear the stale deploy-failed serving state for the promoted version?\n\nThe deploy must be independently confirmed serving, or you will be asked to attest it is live.',
      confirmLabel: 'Clear',
      onConfirm: async () => {
        setLocalDialog(null);
        setActionLoading('clear-deploy-failed');
        setActionMsg(null);
        try {
          const result = await clearDeployFailed(projectId);
          setActionMsg(`Deploy-failed cleared (${result.clear_mode}) → ${result.serving_status ?? 'n/a'}`);
          onRefresh();
          setActionLoading(null);
        } catch (verifyError) {
          const msg = verifyError instanceof Error ? verifyError.message : '';
          // Only offer the attested override when the backend explicitly asked for it.
          if (!msg.includes('force=true')) {
            setActionMsg(msg || 'Request failed');
            setActionLoading(null);
            return;
          }
          setActionLoading(null);
          setLocalDialog({
            title: 'Attest deploy is live',
            message: 'The deploy could not be independently verified live. To force-clear, enter an attestation reason (required).',
            confirmLabel: 'Force clear',
            attestationInput: { label: 'Attestation reason', placeholder: 'e.g. verified live by hand' },
            onConfirm: async (reason?: string) => {
              setLocalDialog(null);
              if (!reason || !reason.trim()) {
                setActionMsg(msg);
                return;
              }
              setActionLoading('clear-deploy-failed');
              try {
                const result = await clearDeployFailed(projectId, true, reason.trim());
                setActionMsg(`Deploy-failed cleared (operator-attested) → ${result.serving_status ?? 'n/a'}`);
                onRefresh();
              } catch (forceError) {
                setActionMsg(forceError instanceof Error ? forceError.message : 'Request failed');
              } finally {
                setActionLoading(null);
              }
            },
          });
        }
      },
    });
  };

  const handleReinstate = projectId
    ? async (criterionKey: string) => {
        try {
          await reinstateAcceptanceCriterion(projectId, criterionKey);
          onRefresh();
        } catch (err) {
          setActionMsg(err instanceof Error ? err.message : 'Failed to reinstate criterion');
        }
      }
    : undefined;

  const handleAttest = projectId
    ? (criterionKey: string) => {
        setLocalDialog({
          title: 'Attest criterion',
          message: `Record an operator attestation that criterion "${criterionKey}" is intentionally accepted despite being stuck.`,
          confirmLabel: 'Attest',
          attestationInput: { label: 'Attestation reason', placeholder: 'e.g. direction retired; criterion superseded by new approach' },
          onConfirm: async (reason?: string) => {
            setLocalDialog(null);
            if (!reason || !reason.trim()) {
              setActionMsg('Attestation reason is required');
              return;
            }
            setActionLoading('attest');
            setActionMsg(null);
            try {
              await attestAcceptanceCriterion(projectId, criterionKey, reason.trim());
              setActionMsg('Criterion attested');
              onRefresh();
            } catch (err) {
              setActionMsg(err instanceof Error ? err.message : 'Failed to attest criterion');
            } finally {
              setActionLoading(null);
            }
          },
        });
      }
    : undefined;

  const handleLinkDirection = projectId
    ? (criterionKey: string) => {
        setLocalDialog({
          title: 'Link criterion to direction',
          message: `Link criterion "${criterionKey}" to a direction by entering the direction key. This replaces the direction's existing criterion links.`,
          confirmLabel: 'Link',
          attestationInput: { label: 'Direction key', placeholder: 'e.g. d-abc12345' },
          onConfirm: async (directionKey?: string) => {
            setLocalDialog(null);
            if (!directionKey || !directionKey.trim()) {
              setActionMsg('Direction key is required');
              return;
            }
            setActionLoading('link-direction');
            setActionMsg(null);
            try {
              await linkCriteriaToDirection(projectId, directionKey.trim(), [criterionKey]);
              setActionMsg('Criterion linked to direction');
              onRefresh();
            } catch (err) {
              setActionMsg(err instanceof Error ? err.message : 'Failed to link criterion');
            } finally {
              setActionLoading(null);
            }
          },
        });
      }
    : undefined;

  return (
    <section data-testid="version-lifecycle-panel">
      <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">Version Lifecycle</h2>
      {refreshError && (
        <VersionLifecycleRefreshBanner kind="stale" message={refreshError} lastSuccessfulRefreshAt={lastSuccessfulRefreshAt} refreshing={refreshing} canRetry={!!projectId} onRefresh={onRefresh} />
      )}
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 space-y-2 text-ui-sm">
        <div className="flex flex-wrap items-center gap-2">
          {data.promotion_enabled !== undefined && (
            <>
              <div className="flex flex-col gap-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`text-ui-sm font-semibold px-1.5 py-0.5 rounded border ${optimisticPromotionEnabled ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400' : 'bg-neutral-500/10 border-neutral-500/30 text-neutral-400'}`} title="Fleet-wide setting (all projects)">
                    {optimisticPromotionEnabled ? 'LIVE' : 'DRY-RUN'}
                  </span>
                  {projectId && userRole === 'admin' && (
                    <button
                      onClick={handleTogglePromotionEnabled}
                      disabled={actionLoading !== null}
                      className="text-ui-sm px-1.5 py-0.5 rounded border border-neutral-500/40 text-neutral-400 hover:bg-neutral-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                      title="Changes promotion mode for all projects"
                      data-viewport-allow-small-target
                    >
                      {actionLoading === 'promotion' ? '…' : optimisticPromotionEnabled ? 'Disable' : 'Enable'}
                    </button>
                  )}
                </div>
                <span className="text-ui-xs text-neutral-600">Fleet-wide setting</span>
              </div>
              {data.promotion_enabled_effective !== undefined && (
                <div className="flex flex-col gap-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className={`text-ui-sm font-semibold px-1.5 py-0.5 rounded border ${data.promotion_enabled_effective ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400' : 'bg-neutral-500/10 border-neutral-500/30 text-neutral-400'}`} title="Effective promotion mode for THIS project">
                      {data.promotion_enabled_effective ? 'LIVE' : 'DRY-RUN'}
                    </span>
                    {projectId && userRole === 'admin' && (
                      <>
                        <button
                          onClick={() => handleSetProjectPromotion(true)}
                          disabled={actionLoading !== null || (data.promotion_enabled_source === 'project' && data.promotion_enabled_override === true)}
                          className="text-ui-sm px-1.5 py-0.5 rounded border border-emerald-500/40 text-emerald-400 hover:bg-emerald-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                          title="Arm live promotion for this project only"
                          data-viewport-allow-small-target
                        >
                          {actionLoading === 'project-promotion' ? '…' : 'Arm'}
                        </button>
                        <button
                          onClick={() => handleSetProjectPromotion(false)}
                          disabled={actionLoading !== null || (data.promotion_enabled_source === 'project' && data.promotion_enabled_override === false)}
                          className="text-ui-sm px-1.5 py-0.5 rounded border border-neutral-500/40 text-neutral-400 hover:bg-neutral-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                          title="Hold live promotion for this project only"
                          data-viewport-allow-small-target
                        >
                          Hold
                        </button>
                        {data.promotion_enabled_source === 'project' && (
                          <button
                            onClick={() => handleSetProjectPromotion(null)}
                            disabled={actionLoading !== null}
                            className="text-ui-sm px-1.5 py-0.5 rounded border border-neutral-500/40 text-neutral-400 hover:bg-neutral-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                            title="Clear this project's override and follow the fleet default"
                            data-viewport-allow-small-target
                          >
                            Use fleet default
                          </button>
                        )}
                      </>
                    )}
                  </div>
                  <span className="text-ui-xs text-neutral-600">
                    This project · {data.promotion_enabled_source === 'project' ? 'project override' : 'fleet default'}
                  </span>
                </div>
              )}
            </>
          )}
        </div>
        {policySource && (
          <PromotionPolicyBadge source={policySource} error={policyError} />
        )}
        {(() => {
          const serving = recent.find(
            v => (v.serving?.status ?? v.serving_status) === 'serving_confirmed',
          );
          return serving ? <PinnedServingVersionCallout version={serving} /> : null;
        })()}
        {cv && (
          <div className="space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-bold text-neutral-200">v{cv.version_number}</span>
              <span className={`text-ui-sm font-semibold px-1.5 py-0.5 rounded border ${cv.phase === 'stabilize' ? 'bg-amber-500/10 border-amber-500/30 text-amber-400' : 'bg-blue-500/10 border-blue-500/30 text-blue-400'}`}>{cv.phase}</span>
              {cv.phase_changed_at && <span className="text-ui-sm text-neutral-500">{timeAgo(cv.phase_changed_at)}</span>}
              <span className="text-ui-sm text-neutral-500">{cv.merged_task_count} tasks merged</span>
              {cv.qualification_attempts > 0 && <span className="text-ui-sm text-neutral-500">{cv.qualification_attempts} lifetime qualification{cv.qualification_attempts !== 1 ? 's' : ''}</span>}
              {cv.active_candidate_sha && (
                <span
                  data-testid="active-candidate-sha"
                  className="text-ui-sm text-neutral-500 font-mono"
                  title={`candidate under evaluation: ${cv.active_candidate_sha}`}
                >
                  candidate @{cv.active_candidate_sha.slice(0, 7)}
                </span>
              )}
            </div>
            {/* What's-new headline for the CURRENT (open) version, matching the
                summary line past versions get when expanded. Gated on highlights so
                older backends that don't emit it degrade gracefully. Status is
                always 'open' here — the lifecycle query only returns the open row. */}
            {cv.highlights && (
              <VersionHighlightsLine
                highlights={cv.highlights}
                phase={cv.phase}
                status="open"
                qualificationState={cv.qualification_state?.state}
              />
            )}
            {editingGoal && projectId ? (
              <div className="flex flex-col gap-1.5">
                <textarea
                  value={goalInput}
                  onChange={(e) => setGoalInput(e.target.value)}
                  placeholder="Enter version goal..."
                  maxLength={500}
                  disabled={actionLoading !== null}
                  className="text-ui-md bg-neutral-800 border border-neutral-700 text-neutral-300 rounded px-2 py-1 focus:outline-none focus:border-neutral-600 disabled:opacity-50 disabled:cursor-not-allowed resize-none"
                  rows={2}
                />
                <div className="flex gap-1">
                  <button
                    onClick={handleSetGoal}
                    disabled={actionLoading !== null}
                    className="text-ui-sm px-2 py-0.5 rounded border border-emerald-500/40 text-emerald-400 hover:bg-emerald-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                    data-viewport-allow-small-target
                  >
                    {actionLoading === 'goal' ? '…' : 'Save'}
                  </button>
                  <button
                    onClick={() => {
                      setEditingGoal(false);
                      setGoalInput(cv.goal || '');
                    }}
                    disabled={actionLoading !== null}
                    className="text-ui-sm px-2 py-0.5 rounded border border-neutral-500/40 text-neutral-400 hover:bg-neutral-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                    data-viewport-allow-small-target
                  >
                    Cancel
                  </button>
                </div>
              </div>
            ) : cv.goal ? (
              <div className="flex flex-col gap-1">
                <p className="text-ui-md text-neutral-300" title={cv.goal}>
                  <span className="text-neutral-500">Goal: </span>{cv.goal}
                </p>
                {cv.goal_updated_at && (
                  <p className="text-ui-sm text-neutral-600">
                    {cv.goal_editor_info && <span>{cv.goal_editor_info} </span>}
                    <span>{timeAgo(cv.goal_updated_at)}</span>
                  </p>
                )}
                {cv.live_current_focus && cv.live_current_focus !== cv.goal && (
                  <div className="flex flex-col gap-1 pt-1 border-t border-amber-500/20">
                    <p className="text-ui-sm px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-400 border border-amber-500/20">
                      Goal has drifted from live focus
                    </p>
                    <p className="text-ui-sm text-neutral-400" title={cv.live_current_focus}>
                      <span className="text-neutral-600">Live focus: </span>{cv.live_current_focus}
                    </p>
                    {projectId && (
                      <button
                        onClick={() => {
                          setGoalInput(cv.live_current_focus || '');
                          setEditingGoal(true);
                        }}
                        disabled={actionLoading !== null}
                        className="text-ui-sm px-1.5 py-0.5 w-fit rounded border border-amber-500/40 text-amber-400 hover:bg-amber-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                        data-viewport-allow-small-target
                      >
                        Resync to focus
                      </button>
                    )}
                  </div>
                )}
                {projectId && (
                  <button
                    onClick={() => setEditingGoal(true)}
                    disabled={actionLoading !== null}
                    className="text-ui-sm px-1.5 py-0.5 w-fit rounded border border-neutral-500/40 text-neutral-400 hover:bg-neutral-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                    data-viewport-allow-small-target
                  >
                    Edit
                  </button>
                )}
              </div>
            ) : projectId ? (
              <button
                onClick={() => setEditingGoal(true)}
                disabled={actionLoading !== null}
                className="text-ui-sm px-1.5 py-0.5 rounded border border-neutral-500/40 text-neutral-400 hover:bg-neutral-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                data-viewport-allow-small-target
              >
                Set goal
              </button>
            ) : null}
            {cv.out_of_band_promotion && (
              <OutOfBandPromotionBadge oob={cv.out_of_band_promotion} />
            )}
            {cv.phase_reasoning && (
              <p className="text-ui-sm text-neutral-400 italic truncate" title={cv.phase_reasoning}>{cv.phase_reasoning}</p>
            )}
            {cv.qualification_state && (
              <div className="pl-2 border-l border-neutral-700/50 space-y-0.5">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`text-ui-sm font-semibold px-1.5 py-0.5 rounded border ${QUAL_STATE_STYLE[cv.qualification_state.state] ?? 'bg-neutral-800 text-neutral-400 border-neutral-700'}`}>
                    {cv.qualification_state.state.replace(/_/g, ' ')}
                  </span>
                  <ServingStatusBadge
                    servingStatus={cv.serving?.status ?? cv.qualification_state?.serving_status}
                    servedSha={cv.serving?.sha ?? cv.served_sha}
                    servedAt={cv.serving?.confirmed_at ?? cv.served_at}
                    deployFailureReason={cv.serving?.failure_reason ?? cv.qualification_state?.deploy_failure_reason}
                    deployConfirmDeadline={cv.deploy_confirm_deadline}
                  />
                  {cv.aggregate_serving_status != null && cv.serving_status != null && cv.aggregate_serving_status !== cv.serving_status && (
                    <span
                      data-testid="serving-disagree-badge"
                      title={`Artifact consensus (aggregate_serving_status: ${cv.aggregate_serving_status}) differs from backend scalar (serving_status: ${cv.serving_status}) — artifact value takes precedence.`}
                      className="text-amber-500 text-ui-sm cursor-help"
                    >
                      ⚠
                    </span>
                  )}
                </div>
                {cv.qualification_state.state === 'promotion_check_wait' && (
                  <p
                    data-testid="promotion-check-wait-freshness"
                    className={`text-ui-sm ${promotionFreshness?.toneClassName ?? 'text-blue-400'}`}
                  >
                    Promotion PR{cv.qualification_state.promotion_pr_number ? ` #${cv.qualification_state.promotion_pr_number}` : ''} in Blueprint&apos;s exact-head check wait{promotionFreshness ? ` (${promotionFreshness.detailText})` : ''} — only the revalidated checked head can merge
                  </p>
                )}
                {cv.qualification_state.state === 'promotion_check_wait' && (
                  <ManagedCheckWaitSnapshotDetails state={cv.qualification_state} />
                )}
                {(cv.qualification_state.state === 'required_check_failed' || cv.qualification_state.state === 'protection_blocked_review') && cv.qualification_state.detail && (
                  <p
                    data-testid="promotion-blocker-detail"
                    className={`text-ui-sm ${cv.qualification_state.state === 'protection_blocked_review' ? 'text-orange-400' : 'text-red-400'}`}
                  >
                    {cv.qualification_state.detail}
                  </p>
                )}
                <p className="text-ui-sm text-neutral-400">{cv.qualification_state.summary}</p>
                <ArtifactEvidenceList artifacts={cv.serving?.artifacts ?? cv.artifacts} agentServingShas={agentServingShas} />
              </div>
            )}
            {cv.last_qualification && (
              <div className="pl-2 border-l border-neutral-700/50 space-y-0.5">
                <div className="flex flex-wrap items-center gap-2">
                  <QualSummary q={cv.last_qualification} />
                  <DebtEnforcePreview q={cv.last_qualification} />
                  {cv.last_qualification.candidate_sha && (() => {
                    const matchedVerdict = verdicts.find(v => v.candidate_sha === cv.last_qualification?.candidate_sha);
                    return matchedVerdict?.terminal_status === 'promoted' ? (
                      <PostPromotionOutcomeBadge outcome={matchedVerdict.post_promotion_outcome} outcomeAt={matchedVerdict.post_promotion_outcome_at} />
                    ) : null;
                  })()}
                  {cv.last_qualification.candidate_sha && (
                    <span
                      className="text-ui-sm text-neutral-500 font-mono"
                      title={cv.last_qualification.candidate_sha}
                    >
                      @{cv.last_qualification.candidate_sha.slice(0, 7)}
                    </span>
                  )}
                  {cv.last_qualification.finalized_at && (
                    <span className="text-ui-sm text-neutral-600">as of {timeAgo(cv.last_qualification.finalized_at)}</span>
                  )}
                </div>
                {(cv.merged_since_verdict ?? 0) > 0 && cv.qualification_state?.state !== 'qualifying' && (
                  <span className="text-ui-sm px-1.5 py-0.5 rounded border bg-amber-500/10 text-amber-400 border-amber-500/20">
                    {cv.merged_since_verdict} task{cv.merged_since_verdict === 1 ? '' : 's'} merged since this verdict — re-qualify for current state
                  </span>
                )}
                {(cv.last_qualification.sub_scores || cv.last_qualification.ci_resolution) && (
                  <div className="flex flex-wrap gap-x-2 gap-y-0.5 text-ui-sm text-neutral-500">
                    {cv.last_qualification.sub_scores && (
                      <>
                        <span>audit {Math.round(cv.last_qualification.sub_scores.audit * 100)}%</span>
                        <span>erosion {Math.round(cv.last_qualification.sub_scores.test_erosion * 100)}%</span>
                        {cv.last_qualification.smoketest_enabled === false
                          ? <span className="text-neutral-600 italic">qualifier smoke retired</span>
                          : typeof cv.last_qualification.sub_scores.smoketest === 'number'
                            ? <span>smoke {Math.round(cv.last_qualification.sub_scores.smoketest * 100)}%</span>
                            : <span className="text-neutral-600 italic">smoke n/a</span>
                        }
                        {cv.last_qualification.sub_scores.debt_pressure != null && (
                          cv.last_qualification.completion_debt?.total_open === 0
                            ? <span className="text-emerald-600">debt: none</span>
                            : <span className={cv.last_qualification.completion_debt != null && cv.last_qualification.completion_debt.total_open > 0 ? 'text-amber-400' : undefined}>
                                debt: {cv.last_qualification.completion_debt?.total_open ?? '?'} open
                              </span>
                        )}
                      </>
                    )}
                    {cv.last_qualification.ci_resolution && (
                      <CiBadge ci={cv.last_qualification.ci_resolution} />
                    )}
                  </div>
                )}
                {cv.last_qualification.completion_debt != null && cv.last_qualification.completion_debt.total_open > 0 && (
                  <div className="text-ui-sm text-neutral-500 pl-2 border-l border-neutral-700/40 space-y-0.5" data-testid="debt-items-list">
                    <span className="text-neutral-400">
                      {cv.last_qualification.completion_debt.total_open} open debt{cv.last_qualification.completion_debt.total_open !== 1 ? 's' : ''} at qualification time
                      {cv.last_qualification.completion_debt.total_open > cv.last_qualification.completion_debt.items.length && (
                        <span className="text-neutral-600"> (showing {cv.last_qualification.completion_debt.items.length} oldest)</span>
                      )}
                    </span>
                    {cv.last_qualification.completion_debt.items.map(item => (
                      <div key={item.id} className="flex gap-x-2 text-neutral-500">
                        <span className="font-mono text-ui-xs text-neutral-600">{item.kind}</span>
                        <span className="truncate">{item.title}</span>
                        {item.age_days != null && <span className="text-neutral-600 shrink-0">{item.age_days}d</span>}
                      </div>
                    ))}
                  </div>
                )}
                {cv.dimension_states && cv.dimension_states.length > 0 && (
                  <DimensionBreakdown dimensions={cv.dimension_states} />
                )}
                {cv.last_qualification.blocker && (
                  <p className="text-ui-sm text-red-400 truncate">{cv.last_qualification.blocker}</p>
                )}
                {(cv.last_qualification.audit_missing_reason || (cv.last_qualification.consecutive_infra_errors ?? 0) > 0 || (cv.last_qualification.status === 'infra_failure' && cv.last_qualification.error)) && (
                  <div className="flex flex-wrap gap-1.5">
                    {cv.last_qualification.audit_missing_reason && (
                      <span className="text-ui-sm px-1.5 py-0.5 rounded border bg-amber-500/10 text-amber-400 border-amber-500/20">
                        {AUDIT_MISSING_REASON_LABELS[cv.last_qualification.audit_missing_reason] ?? cv.last_qualification.audit_missing_reason}
                      </span>
                    )}
                    {(cv.last_qualification.consecutive_infra_errors ?? 0) > 0 && (
                      <span className={`text-ui-sm px-1.5 py-0.5 rounded border ${(cv.last_qualification.consecutive_infra_errors ?? 0) >= 3 ? 'bg-red-500/10 text-red-400 border-red-500/20' : 'bg-orange-500/10 text-orange-400 border-orange-500/20'}`}>
                        {cv.last_qualification.consecutive_infra_errors} consecutive infra failures
                      </span>
                    )}
                    {cv.last_qualification.status === 'infra_failure' && cv.last_qualification.error && (
                      <span className="text-ui-sm px-1.5 py-0.5 rounded border bg-red-500/10 text-red-400 border-red-500/20">
                        {cv.last_qualification.error}
                      </span>
                    )}
                  </div>
                )}
                {cv.last_qualification.is_permanent_gate_incapacity && projectId && (
                  <div className="mt-1 pl-2 border-l-2 border-red-500/40 space-y-2" data-testid="hotfix-remediation-panel">
                    <p className="text-ui-sm text-red-400 font-medium">
                      Permanent gate incapacity — the release is held by a structural gate failure.
                      The operator hotfix-deploy channel may unblock it.
                    </p>
                    {!hotfixPanelOpen ? (
                      <button
                        onClick={() => {
                          setHotfixPanelOpen(true);
                          setHotfixPreviewLoading(true);
                          getHotfixPreview(projectId, cv.active_candidate_sha)
                            .then(r => { setHotfixPreview(r); })
                            .catch(e => { setHotfixPreview({ status: 'error', eligible: false, changed_files: [], offending_files: [], reason: String(e?.message ?? e) }); })
                            .finally(() => setHotfixPreviewLoading(false));
                        }}
                        className="text-ui-sm px-2 py-0.5 min-h-[44px] sm:min-h-0 rounded border border-red-500/40 text-red-400 hover:bg-red-500/10"
                        data-testid="hotfix-open-btn"
                      >
                        Inspect hotfix eligibility
                      </button>
                    ) : (
                      <div className="space-y-2">
                        <div className="text-ui-sm space-y-1">
                          <div className="flex gap-2 text-neutral-500">
                            <span>Candidate:</span>
                            <span className="font-mono text-neutral-400">{cv.active_candidate_sha?.slice(0, 12) ?? '—'}</span>
                            {hotfixPreview?.prior_deployed_sha && (
                              <>
                                <span>Serving:</span>
                                <span className="font-mono text-neutral-400">{hotfixPreview.prior_deployed_sha.slice(0, 12)}</span>
                              </>
                            )}
                          </div>
                          {hotfixPreviewLoading && <p className="text-neutral-500 italic">Loading scope preview…</p>}
                          {hotfixPreview && !hotfixPreviewLoading && (
                            <>
                              <p className={`font-medium ${hotfixPreview.eligible ? 'text-emerald-400' : 'text-red-400'}`}>
                                {hotfixPreview.eligible ? 'Eligible for hotfix-deploy' : 'Not eligible for hotfix-deploy'}
                              </p>
                              <p className="text-neutral-500">{hotfixPreview.reason}</p>
                              {hotfixPreview.changed_files.length > 0 && (
                                <div>
                                  <p className="text-neutral-500">Changed files ({hotfixPreview.changed_files.length}):</p>
                                  <ul className="pl-2 space-y-0.5">
                                    {hotfixPreview.changed_files.map(f => (
                                      <li key={f} className={`font-mono text-ui-xs ${hotfixPreview.offending_files.includes(f) ? 'text-red-400' : 'text-neutral-400'}`}>
                                        {hotfixPreview.offending_files.includes(f) ? '✗ ' : '✓ '}{f}
                                      </li>
                                    ))}
                                  </ul>
                                </div>
                              )}
                              {hotfixPreview.eligible && (
                                <div className="space-y-2 pt-1 border-t border-neutral-700/40">
                                  <p className="text-neutral-400 text-ui-sm">
                                    This will ship only the allowlisted release-machinery files. CI and
                                    migration guards still apply. The fix is INERT until the deployment completes.
                                  </p>
                                  <div>
                                    <label className="block text-ui-sm text-neutral-400 mb-0.5">Hotfix reason (required)</label>
                                    <input
                                      type="text"
                                      value={hotfixReason}
                                      onChange={e => setHotfixReason(e.target.value)}
                                      placeholder="Describe what this hotfix fixes and why it is safe"
                                      className="w-full bg-neutral-800 border border-neutral-600 rounded px-2 py-1 text-ui-sm text-neutral-200 placeholder:text-neutral-600"
                                      data-testid="hotfix-reason-input"
                                    />
                                  </div>
                                  <label className="flex items-start gap-2 text-ui-sm text-neutral-400 cursor-pointer">
                                    <input
                                      type="checkbox"
                                      checked={hotfixAck}
                                      onChange={e => setHotfixAck(e.target.checked)}
                                      className="mt-0.5 shrink-0"
                                      data-testid="hotfix-ack-checkbox"
                                    />
                                    I understand this fix is inert until the promoted content is deployed.
                                  </label>
                                  <button
                                    disabled={!hotfixReason.trim() || !hotfixAck || actionLoading !== null}
                                    onClick={async () => {
                                      if (!hotfixReason.trim() || !hotfixAck) return;
                                      setActionLoading('hotfix');
                                      try {
                                        await triggerHotfixPromote(
                                          projectId,
                                          hotfixReason.trim(),
                                          hotfixPreview.changed_files,
                                          true,
                                          cv.active_candidate_sha,
                                        );
                                        setActionMsg('Hotfix-promote initiated.');
                                        setHotfixPanelOpen(false);
                                        setHotfixReason('');
                                        setHotfixAck(false);
                                        setHotfixPreview(null);
                                        onRefresh?.();
                                      } catch (e: unknown) {
                                        setActionMsg(`Hotfix failed: ${(e as Error)?.message ?? String(e)}`);
                                      } finally {
                                        setActionLoading(null);
                                      }
                                    }}
                                    className="text-ui-sm px-3 py-1 rounded border border-red-500/50 text-red-400 hover:bg-red-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                                    data-testid="hotfix-promote-btn"
                                  >
                                    {actionLoading === 'hotfix' ? '…' : 'Hotfix-promote'}
                                  </button>
                                </div>
                              )}
                            </>
                          )}
                        </div>
                        <button
                          onClick={() => { setHotfixPanelOpen(false); setHotfixPreview(null); }}
                          className="text-ui-sm text-neutral-500 hover:text-neutral-300"
                          data-testid="hotfix-close-btn"
                        >
                          Close
                        </button>
                      </div>
                    )}
                  </div>
                )}
                {cv.last_qualification.audit_reconciliation && (
                  <AuditReconciliationRow rec={cv.last_qualification.audit_reconciliation} />
                )}
                <DurableQualificationEvidenceRow q={cv.last_qualification} />
              </div>
            )}
            {cv.qualification_trigger_state && (
              <QualificationTriggerRow state={cv.qualification_trigger_state} />
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
            {cv.tasks_merged !== undefined && (
              <div>
                <button
                  onClick={() => setChangelogExpanded(v => !v)}
                  className="flex items-center gap-1.5 text-ui-sm text-neutral-400 hover:text-neutral-200 transition-colors py-0.5"
                  data-testid="changelog-toggle"
                  data-viewport-allow-small-target
                >
                  <span>{changelogExpanded ? '▾' : '▸'}</span>
                  <span>What shipped</span>
                  <span className="text-neutral-600">
                    ({cv.merged_task_count} task{cv.merged_task_count !== 1 ? 's' : ''}{cv.tasks_merged_has_more ? ', showing 50 newest' : ''})
                  </span>
                </button>
                {changelogExpanded && (
                  <VersionSummaryPanel tasksMerged={cv.tasks_merged} acceptanceAggregate={cv.acceptance_aggregate} criterionCoverage={cv.criterion_coverage} onReinstate={handleReinstate} onAttest={handleAttest} onLinkDirection={handleLinkDirection} />
                )}
              </div>
            )}
            {projectId && (
              <div data-testid="phase-controls" className="flex flex-wrap items-center gap-2 pt-1">
                {cv.phase === 'build' ? (
                  <button
                    onClick={() => handleSetPhase('stabilize')}
                    disabled={actionLoading !== null || promotionLocked}
                    title={phaseLockTitle}
                    className="text-ui-sm px-2 py-0.5 min-h-[44px] sm:min-h-0 rounded border border-amber-500/40 text-amber-400 hover:bg-amber-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {actionLoading === 'phase' ? '…' : '→ stabilize'}
                  </button>
                ) : (
                  <button
                    onClick={() => handleSetPhase('build')}
                    disabled={actionLoading !== null || promotionLocked}
                    title={phaseLockTitle}
                    className="text-ui-sm px-2 py-0.5 min-h-[44px] sm:min-h-0 rounded border border-blue-500/40 text-blue-400 hover:bg-blue-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {actionLoading === 'phase' ? '…' : '→ build'}
                  </button>
                )}
                {cv.phase === 'stabilize' && (
                  <button
                    onClick={handleQualify}
                    disabled={actionLoading !== null || promotionLocked}
                    title={qualifyLockTitle}
                    className="text-ui-sm px-2 py-0.5 min-h-[44px] sm:min-h-0 rounded border border-emerald-500/40 text-emerald-400 hover:bg-emerald-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {actionLoading === 'qualify' ? '…' : 'Run Qualify'}
                  </button>
                )}
                {promotionCheckWait && (
                  <span
                    data-testid="promotion-check-wait-note"
                    className={`text-ui-sm italic ${promotionFreshness?.toneClassName ?? 'text-blue-300'}`}
                  >
                    Promotion PR{cv.qualification_state?.promotion_pr_number ? ` #${cv.qualification_state.promotion_pr_number}` : ''} open in Blueprint&apos;s exact-head check wait{promotionLockContext}; Blueprint revalidates the target and merges only the checked head after required checks pass. Qualify &amp; phase locked until it lands or is canceled — no fresh qualification needed.
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
                        className="text-ui-sm flex-1 bg-neutral-800 border border-neutral-700 text-neutral-300 rounded px-2 py-0.5 focus:outline-none focus:border-neutral-600 disabled:opacity-50 disabled:cursor-not-allowed"
                      />
                      <button
                        onClick={handleCutVersion}
                        disabled={actionLoading !== null}
                        className="text-ui-sm px-2 py-0.5 rounded border border-red-500/40 text-red-400 hover:bg-red-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                        data-viewport-allow-small-target
                      >
                        {actionLoading === 'cut' ? '…' : 'Cut'}
                      </button>
                      <button
                        onClick={() => {
                          setCuttingVersion(false);
                          setCutReason('');
                        }}
                        disabled={actionLoading !== null}
                        className="text-ui-sm px-2 py-0.5 rounded border border-neutral-500/40 text-neutral-400 hover:bg-neutral-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                        data-viewport-allow-small-target
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                ) : (
                  <button
                    onClick={() => setCuttingVersion(true)}
                    disabled={actionLoading !== null}
                    className="text-ui-sm px-2 py-0.5 min-h-[44px] sm:min-h-0 rounded border border-red-500/40 text-red-400 hover:bg-red-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
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
                    className="text-ui-sm px-2 py-0.5 min-h-[44px] sm:min-h-0 rounded border border-orange-500/40 text-orange-400 hover:bg-orange-500/10 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {actionLoading === 'clear-deploy-failed' ? '…' : 'Clear deploy-failed'}
                  </button>
                )}
                {actionMsg && (
                  <span className="text-ui-sm text-neutral-400 italic">{actionMsg}</span>
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
        {verdictsError && (
          <p data-testid="verdicts-error" className="text-ui-sm text-red-400">{verdictsError}</p>
        )}
        {verdicts.length > 0 && (
          <div data-testid="verdict-history">
            <p className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-1">Verdict History</p>
            <ConfidenceSparkline verdicts={verdicts.slice(0, 8)} />
            <div className="space-y-1">
              {verdicts.slice(0, 8).map(v => (
                <div key={v.id} className="space-y-0.5 pl-2 border-l-2 border-neutral-700/40">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-ui-sm">
                    <span className={`font-mono font-semibold ${v.would_promote ? 'text-emerald-400' : 'text-amber-400'}`}>{v.confidence != null ? `${Math.round(v.confidence * 100)}%` : '—'}</span>
                    <span className={QUAL_VERDICT_COLOR[v.terminal_status ?? ''] ?? 'text-neutral-400'}>{v.terminal_status?.replace(/_/g, '-') ?? '?'}</span>
                    {v.promotion_enabled && <span className="text-ui-xs font-mono text-emerald-600">LIVE</span>}
                    <DebtEnforcePreview q={v} />
                    {v.sub_scores && <span className="text-neutral-600 font-mono text-ui-xs">a:{Math.round(v.sub_scores.audit * 100)} e:{Math.round(v.sub_scores.test_erosion * 100)}{v.smoketest_enabled === false ? ' s:retired' : typeof v.sub_scores.smoketest === 'number' ? ` s:${Math.round(v.sub_scores.smoketest * 100)}` : ' s:n/a'}{v.sub_scores.debt_pressure != null ? ` d:${v.completion_debt?.total_open ?? '?'}open` : ''}</span>}
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
            <p className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-1">History</p>
            <div className="space-y-1">
              {recent.slice(0, 4).map((v: RecentVersion) => {
                const isExpanded = !!v.id && expandedVersionId === v.id;
                return (
                  <div key={v.version_number} className="space-y-0.5">
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-ui-sm">
                      <span className="text-neutral-400 font-mono w-8">v{v.version_number}</span>
                      <span className="text-neutral-500">{v.status}</span>
                      <ServingStatusBadge
                        servingStatus={v.serving?.status ?? v.qualification_state?.serving_status}
                        servedSha={v.serving?.sha ?? v.served_sha}
                        servedAt={v.serving?.confirmed_at ?? v.served_at}
                        deployFailureReason={v.serving?.failure_reason ?? v.qualification_state?.deploy_failure_reason}
                        deployConfirmDeadline={v.deploy_confirm_deadline}
                      />
                      {v.aggregate_serving_status != null && v.serving_status != null && v.aggregate_serving_status !== v.serving_status && (
                        <span
                          data-testid="serving-disagree-badge"
                          title={`Artifact consensus (aggregate_serving_status: ${v.aggregate_serving_status}) differs from backend scalar (serving_status: ${v.serving_status}) — artifact value takes precedence.`}
                          className="text-amber-500 text-ui-sm cursor-help"
                        >
                          ⚠
                        </span>
                      )}
                      <QualSummary q={v.last_qualification} />
                      {v.status === 'abandoned' && (v.abandoned_by || v.abandon_reason || v.abandoned_at) && (
                        <span className="text-neutral-400">
                          cut by {v.abandoned_by ?? 'unknown'}
                          {v.abandon_reason ? `: ${v.abandon_reason}` : ''}
                          {v.abandoned_at ? ` · ${timeAgo(v.abandoned_at)}` : ''}
                        </span>
                      )}
                      {v.id && (
                        <button
                          onClick={() => setExpandedVersionId(isExpanded ? null : v.id ?? null)}
                          className="min-h-[44px] min-w-[44px] flex items-center justify-center text-neutral-600 hover:text-neutral-300 transition-colors"
                          title={isExpanded ? 'Hide summary' : "What's new"}
                        >
                          {isExpanded ? '▲' : '▼'}
                        </button>
                      )}
                    </div>
                    {v.out_of_band_promotion && (
                      <OutOfBandPromotionBadge oob={v.out_of_band_promotion} />
                    )}
                    {!isExpanded && v.highlights && (
                      <VersionHighlightsLine
                        highlights={v.highlights}
                        phase={v.phase}
                        status={v.status}
                        qualificationState={v.qualification_state?.state}
                      />
                    )}
                    <ArtifactEvidenceList artifacts={v.artifacts} agentServingShas={agentServingShas} />
                    {isExpanded && v.id && <VersionHistorySummary versionId={v.id} />}
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>
      {localDialog && (
        <ConfirmDialog
          title={localDialog.title}
          message={localDialog.message}
          confirmLabel={localDialog.confirmLabel}
          attestationInput={localDialog.attestationInput}
          onConfirm={localDialog.onConfirm}
          onCancel={() => setLocalDialog(null)}
        />
      )}
    </section>
  );
}
