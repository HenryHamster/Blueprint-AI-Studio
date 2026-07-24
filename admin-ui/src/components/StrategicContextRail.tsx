import { memo, useMemo, useState, type ReactNode } from 'react';
import { Activity, ArrowUpRight, CircleAlert, Cpu, CreditCard, GitBranch, Network, Radio, X } from 'lucide-react';
import { acceptPendingConfirmation, dismissPendingConfirmation } from '../api';
import type { PendingConfirmation, PendingConfirmationType } from '../types';
import { PendingConfirmationList } from './ProblemModelAttention';
import type { StrategicCycleCompletion, StrategicCycleProgress } from '../hooks/useLogViewer';
import type {
  StrategicAccountSpendStatus,
  StrategicContextPackageDescriptor,
  StrategicContextPackageDrilldown,
  StrategicLocalRunnerState,
  StrategicMessageTarget,
  StrategicStateTaskFocus,
  StrategicStateTaskFocusBucket,
  StrategicStateTaskFocusItem,
  StrategicRuntimeWorkState,
  StrategicStateAttentionItem,
  StrategicStateResponse,
  StrategicWorkbenchState,
  StrategicWorkSessionAction,
  StrategicWorkSessionEvent,
  StrategicWorkSessionRefDescriptor,
} from '../strategicStateApi';
import {
  MAX_STRATEGIC_SELECTED_REFS,
  removeStrategicSelectedRef,
  type StrategicSelectedRef,
} from '../utils/strategicWorkbenchSession';
import { WorkbenchPanel } from './WorkbenchLayout';
import { RailMiniCanvas } from './RailMiniCanvas';
import { StrategicSessionEvents } from './StrategicSessionEvents';
import { WhiteboardRailSection } from './WhiteboardRailSection';

type ProjectTarget = 'running-update' | 'directions' | 'problem-model' | 'design-doc';
type TaskFocusBucketKey = 'needs_judgment' | 'active' | 'friction' | 'queued' | 'recent_completed';

function isProjectTarget(value?: string | null): value is ProjectTarget {
  return value === 'running-update' || value === 'directions' || value === 'problem-model' || value === 'design-doc';
}

interface StrategicContextRailProps {
  state: StrategicStateResponse | null;
  loading?: boolean;
  error?: string | null;
  strategistActive?: boolean;
  cycleProgress?: StrategicCycleProgress | null;
  lastCycleCompletion?: StrategicCycleCompletion | null;
  onOpenProjectSection: (section: ProjectTarget) => void;
  onOpenPipeline: () => void;
  onOpenRuntimeHealth: () => void;
  onOpenRuntimeDevices?: () => void;
  onOpenRuntimeCompute?: () => void;
  onOpenAccounts?: () => void;
  onOpenSpend?: () => void;
  onOpenContextWorkspace?: () => void;
  onOpenTask?: (taskId: string) => void;
  onOpenMessages?: (target?: StrategicMessageTarget) => void;
  onOpenKnowledge?: (documentId?: string | null, directionId?: string | null) => void;
  onOpenTarget?: (target?: StrategicContextPackageDrilldown['target'] | null) => void;
  workbenchState?: StrategicWorkbenchState | null;
  accountSpend?: StrategicAccountSpendStatus | null;
  savingSessionRefs?: boolean;
  onUpdateSessionRefs?: (
    updateRefs: (refs: StrategicWorkbenchState['selected_refs']) => StrategicWorkbenchState['selected_refs']
  ) => void;
  /** Called after a pending-confirmation accept/dismiss succeeds so the parent
      can refresh strategic state (which re-derives attention). */
  onConfirmationResolved?: () => void;
  addToast?: (message: string, type: 'success' | 'error' | 'info') => void;
  /** Project whose whiteboard/canvas the strategic sidebar surfaces. */
  projectId?: string | null;
}

function formatCost(value: number | undefined): string {
  const n = Number.isFinite(value) ? Number(value) : 0;
  return `$${n.toFixed(2)}`;
}

function formatCompactNumber(value: number | null | undefined): string {
  const n = Number(value ?? 0);
  if (!Number.isFinite(n)) return '0';
  if (Math.abs(n) >= 1000) return `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}k`;
  return String(n);
}

function phaseLabel(phase?: string | null): string {
  if (phase === 'strategic_review') return 'Strategic review';
  if (phase === 'design_doc_update') return 'Architecture review';
  return 'Strategic';
}

function contextPackageTone(status?: string | null): { dot: string; text: string; label: string } {
  if (status === 'building') return { dot: 'bg-cyan-400', text: 'text-cyan-300', label: 'building' };
  if (status === 'stale') return { dot: 'bg-amber-300', text: 'text-amber-300', label: 'stale' };
  return { dot: 'bg-emerald-400', text: 'text-emerald-300', label: 'ready' };
}

function pluralize(count: number, singular: string): string {
  return `${count} ${singular}${count === 1 ? '' : 's'}`;
}

function shortId(value?: string | null): string | null {
  return value ? value.slice(0, 8) : null;
}

function localRunnerTone(localRunner: StrategicLocalRunnerState): { dot: string; text: string; label: string } {
  if (localRunner.mode === 'local') return { dot: 'bg-emerald-400', text: 'text-emerald-300', label: 'local' };
  if (localRunner.mode === 'local_ready') return { dot: 'bg-emerald-400', text: 'text-emerald-300', label: 'ready' };
  if (localRunner.mode === 'pending_local') return { dot: 'bg-amber-300', text: 'text-amber-300', label: 'waiting' };
  if (localRunner.mode === 'cloud') return { dot: 'bg-neutral-600', text: 'text-neutral-400', label: 'cloud' };
  return { dot: 'bg-neutral-600', text: 'text-neutral-400', label: 'unknown' };
}

function localRunnerDetail(localRunner: StrategicLocalRunnerState): string {
  if (localRunner.mode === 'local') {
    const parts = [
      shortId(localRunner.active_runner_id) ? `runner ${shortId(localRunner.active_runner_id)}` : null,
      localRunner.active_task_runtime?.model ?? null,
    ].filter(Boolean);
    return parts.length > 0 ? `Running on ${parts.join(' / ')}` : 'Current Strategic cycle is pinned locally';
  }
  if (localRunner.eligible_live_count > 0) {
    return `${pluralize(localRunner.eligible_live_count, 'runner')} ready for Strategic work`;
  }
  if (localRunner.fallback_reason === 'no_runner') return 'No local Strategic runner enrolled';
  if (localRunner.fallback_reason === 'runner_offline') {
    return `${pluralize(localRunner.strategic_runner_count, 'runner')} enrolled, none live`;
  }
  if (localRunner.fallback_reason === 'agent_unavailable') return 'Runner live, agent unavailable';
  if (localRunner.fallback_reason === 'at_capacity') return 'Runner busy with active work';
  if (localRunner.fallback_reason === 'cloud_dispatch') return 'Current cycle already dispatched outside the runner';
  if (localRunner.fallback_reason === 'capability_mismatch') return 'Runner does not cover the full Strategic cycle';
  if (localRunner.fallback_reason === 'runner_claim_pending') return 'Pinned locally; waiting for runner claim';
  return 'Local runner is not available for Strategic work';
}

function runtimeWorkTone(work: StrategicRuntimeWorkState): { dot: string; text: string; label: string } {
  if (
    (work.local_runner_holds?.count ?? 0) > 0 ||
    (work.held_approvals?.count ?? 0) > 0 ||
    (work.parked_wakes?.counts.problem ?? 0) > 0
  ) {
    return { dot: 'bg-amber-300', text: 'text-amber-300', label: 'needs check' };
  }
  if ((work.compute_jobs?.counts.failed_recent ?? 0) > 0 || (work.compute_jobs?.counts.timed_out_recent ?? 0) > 0) {
    return { dot: 'bg-rose-400', text: 'text-rose-300', label: 'recent failure' };
  }
  if ((work.compute_jobs?.counts.active ?? 0) > 0 || (work.parked_wakes?.counts.active ?? 0) > 0) {
    return { dot: 'bg-cyan-400', text: 'text-cyan-300', label: 'active' };
  }
  if ((work.compute_jobs?.counts.queued ?? 0) > 0) {
    return { dot: 'bg-cyan-400', text: 'text-cyan-300', label: 'queued' };
  }
  if ((work.compute_jobs?.counts.terminal_recent ?? 0) > 0) {
    return { dot: 'bg-neutral-500', text: 'text-neutral-400', label: 'recent result' };
  }
  return { dot: 'bg-neutral-600', text: 'text-neutral-400', label: 'quiet' };
}

function runtimeWorkDetail(work: StrategicRuntimeWorkState): string {
  const localRunnerHoldCount = work.local_runner_holds?.count ?? 0;
  const parts = [
    (work.compute_jobs?.counts.active ?? 0) > 0
      ? pluralize(work.compute_jobs.counts.active, 'compute job')
      : null,
    (work.compute_jobs?.counts.queued ?? 0) > 0
      ? pluralize(work.compute_jobs.counts.queued, 'queued compute job')
      : null,
    localRunnerHoldCount > 0
      ? pluralize(localRunnerHoldCount, 'local runner hold')
      : null,
    (work.held_approvals?.count ?? 0) > 0
      ? pluralize(work.held_approvals.count, 'approval')
      : null,
    (work.parked_wakes?.counts.active ?? 0) > 0
      ? pluralize(work.parked_wakes.counts.active, 'parked wake')
      : null,
    (work.compute_jobs?.counts.failed_recent ?? 0) > 0
      ? pluralize(work.compute_jobs.counts.failed_recent, 'recent failure')
      : null,
  ].filter(Boolean);
  if (parts.length > 0) return parts.join(', ');
  const terminal = work.compute_jobs?.counts.terminal_recent ?? 0;
  return terminal > 0 ? `${pluralize(terminal, 'recent compute result')}` : 'No active background work';
}

function accountSpendTone(accountSpend: StrategicAccountSpendStatus): { dot: string; text: string; label: string } {
  const total = accountSpend.totalAccounts ?? null;
  const available = accountSpend.availableAccounts ?? null;
  const enabled = accountSpend.enabledAccounts ?? null;
  if (total === 0) return { dot: 'bg-neutral-600', text: 'text-neutral-400', label: 'not configured' };
  if (available === 0 && (total == null || total > 0)) return { dot: 'bg-rose-400', text: 'text-rose-300', label: 'blocked' };
  if (
    (accountSpend.cooldownAccounts ?? 0) > 0 ||
    (accountSpend.cappedAccounts ?? 0) > 0 ||
    (accountSpend.disabledModelCount ?? 0) > 0 ||
    (available != null && total != null && available < total)
  ) {
    return { dot: 'bg-amber-300', text: 'text-amber-300', label: 'needs check' };
  }
  if ((available ?? 0) > 0 || (enabled ?? 0) > 0) return { dot: 'bg-emerald-400', text: 'text-emerald-300', label: 'available' };
  return { dot: 'bg-neutral-600', text: 'text-neutral-400', label: 'unknown' };
}

function accountSpendDetail(accountSpend: StrategicAccountSpendStatus): string {
  const total = accountSpend.totalAccounts ?? null;
  const available = accountSpend.availableAccounts ?? null;
  const enabled = accountSpend.enabledAccounts ?? null;
  const trackedValue = accountSpend.trackedSpendUsd ?? accountSpend.cumulativeCostUsd ?? null;
  const ceilingValue = accountSpend.dailyCostCeilingUsd ?? null;
  const trackedSpend = typeof trackedValue === 'number' && Number.isFinite(trackedValue) && trackedValue > 0
    ? formatCost(trackedValue)
    : null;
  const ceiling = typeof ceilingValue === 'number' && Number.isFinite(ceilingValue) && ceilingValue > 0
    ? formatCost(ceilingValue)
    : null;
  const parts = [
    total === 0 ? 'No accounts configured' : null,
    available != null && total != null && total > 0 ? `${available}/${total} available` : null,
    enabled != null && total == null ? pluralize(enabled, 'enabled account') : null,
    enabled != null && total != null && total > 0 && enabled !== available ? pluralize(enabled, 'enabled account') : null,
    (accountSpend.activeAccountTasks ?? 0) > 0 ? `${accountSpend.activeAccountTasks} active` : null,
    (accountSpend.cooldownAccounts ?? 0) > 0 ? `${accountSpend.cooldownAccounts} cooldown` : null,
    (accountSpend.configuredCapAccounts ?? 0) > 0 ? `${accountSpend.configuredCapAccounts} with caps` : null,
    (accountSpend.disabledModelCount ?? 0) > 0 ? pluralize(accountSpend.disabledModelCount ?? 0, 'disabled model') : null,
    trackedSpend ? `tracked ${trackedSpend}` : null,
    ceiling ? `daily ceiling ${ceiling}` : null,
  ].filter(Boolean);
  return parts.length > 0 ? parts.join(' / ') : 'Account and spend status unavailable';
}

function accountSpendHasProblem(
  accountSpend: StrategicAccountSpendStatus,
  status: ReturnType<typeof accountSpendTone>,
): boolean {
  if (status.label !== 'available') return true;
  return (
    (accountSpend.cooldownAccounts ?? 0) > 0 ||
    (accountSpend.cappedAccounts ?? 0) > 0 ||
    (accountSpend.disabledModelCount ?? 0) > 0 ||
    (accountSpend.availableAccounts != null &&
      accountSpend.totalAccounts != null &&
      accountSpend.availableAccounts < accountSpend.totalAccounts)
  );
}

function shaFreshnessTone(verdict?: string | null): { dot: string; text: string; label: string } {
  if (verdict === 'fresh') return { dot: 'bg-emerald-400', text: 'text-emerald-300', label: 'fresh' };
  if (verdict === 'stale') return { dot: 'bg-rose-400', text: 'text-rose-300', label: 'stale' };
  return { dot: 'bg-neutral-600', text: 'text-neutral-400', label: 'unknown' };
}

function ShaFreshnessBlock({
  shaFreshness,
}: {
  shaFreshness: StrategicStateResponse['sha_freshness'];
}) {
  if (!shaFreshness) return null;
  const tone = shaFreshnessTone(shaFreshness.verdict);
  const candidateSha = shaFreshness.active_candidate_sha?.slice(0, 7) ?? null;
  const integrationSha = shaFreshness.integration_head_sha?.slice(0, 7) ?? null;
  const branch = shaFreshness.integration_branch ?? null;
  const checkedAt = shaFreshness.checked_at ?? null;
  return (
    <div className="mt-3 border-t border-neutral-800 pt-3" data-testid="sha-freshness-block">
      <div className="flex min-w-0 items-center gap-2">
        <GitBranch className="h-4 w-4 shrink-0 text-neutral-500" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">
            <span className="truncate text-xs font-semibold text-neutral-100">Payload freshness</span>
            <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${tone.dot}`} aria-hidden="true" />
            <span className={`shrink-0 text-ui-sm font-semibold uppercase ${tone.text}`} data-testid="freshness-verdict">
              {tone.label}
            </span>
          </div>
          {(candidateSha || integrationSha || branch) && (
            <p className="mt-1 truncate text-xs leading-relaxed text-neutral-500" data-testid="freshness-sha-detail">
              {candidateSha && `candidate @${candidateSha}`}
              {candidateSha && integrationSha && ' · '}
              {integrationSha && `head @${integrationSha}`}
              {(candidateSha || integrationSha) && branch ? ` on ${branch}` : null}
              {!(candidateSha || integrationSha) && branch ? `branch ${branch}` : null}
            </p>
          )}
          {checkedAt && (
            <p className="mt-0.5 truncate text-ui-sm text-neutral-600" data-testid="freshness-checked-at">
              as of {checkedAt}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

function runtimeWorkHasSignal(work?: StrategicRuntimeWorkState | null): work is StrategicRuntimeWorkState {
  if (!work) return false;
  return (
    (work.compute_jobs?.counts.active ?? 0) > 0 ||
    (work.compute_jobs?.counts.queued ?? 0) > 0 ||
    (work.compute_jobs?.counts.failed_recent ?? 0) > 0 ||
    (work.compute_jobs?.counts.timed_out_recent ?? 0) > 0 ||
    (work.local_runner_holds?.count ?? 0) > 0 ||
    (work.held_approvals?.count ?? 0) > 0 ||
    (work.parked_wakes?.counts.active ?? 0) > 0 ||
    (work.parked_wakes?.counts.problem ?? 0) > 0
  );
}

function workSessionEventHasActionableSignal(event: StrategicWorkSessionEvent): boolean {
  const tone = event.tone?.toLowerCase();
  if (tone === 'critical' || tone === 'warning') return true;
  return event.target?.surface === 'messages' && event.target.action === 'reply';
}

function RailButton({
  children,
  onClick,
  ariaLabel,
  title,
}: {
  children: ReactNode;
  onClick: () => void;
  ariaLabel?: string;
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={ariaLabel}
      title={title ?? ariaLabel}
      className="inline-flex min-h-[44px] items-center gap-1.5 rounded-md border border-neutral-800 bg-neutral-950/70 px-2.5 text-xs font-semibold text-neutral-300 transition-colors hover:border-neutral-700 hover:bg-neutral-900 hover:text-neutral-100"
    >
      <span>{children}</span>
      <ArrowUpRight className="h-3.5 w-3.5 text-neutral-500" aria-hidden="true" />
    </button>
  );
}

function StatRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex min-w-0 items-center justify-between gap-3 py-1">
      <span className="truncate text-xs text-neutral-500">{label}</span>
      <span className="min-w-0 truncate text-right text-xs font-semibold text-neutral-200">{value}</span>
    </div>
  );
}

function resourceKindLabel(kind?: string | null): string {
  if (kind === 'canvas') return 'map';
  if (kind === 'knowledge_document') return 'knowledge';
  if (kind === 'artifact') return 'artifact';
  if (kind === 'task') return 'task';
  return (kind || 'resource').replace(/_/g, ' ');
}

function sessionRefKey(ref: Pick<StrategicSelectedRef, 'kind' | 'id'>): string {
  return `${ref.kind}:${ref.id}`;
}

function fallbackRefDescriptor(ref: StrategicSelectedRef): StrategicWorkSessionRefDescriptor {
  return {
    kind: ref.kind,
    id: ref.id,
    label: ref.label || `${resourceKindLabel(ref.kind)} ${ref.id.slice(0, 8)}`,
  };
}

function SessionContextList({
  refs,
  onOpenTarget,
  onRemove,
  disabled,
}: {
  refs: StrategicWorkSessionRefDescriptor[];
  onOpenTarget: (target?: StrategicContextPackageDrilldown['target'] | null) => void;
  onRemove?: (ref: Pick<StrategicSelectedRef, 'kind' | 'id'>) => void;
  disabled?: boolean;
}) {
  if (refs.length === 0) return null;
  return (
    <div className="mt-2 space-y-1.5" data-testid="strategic-session-refs">
      <div className="text-ui-sm font-semibold uppercase text-neutral-500">Session context</div>
      {refs.slice(0, MAX_STRATEGIC_SELECTED_REFS).map(ref => {
        const canOpen = Boolean(ref.target);
        return (
          <div
            key={sessionRefKey(ref)}
            className="flex min-w-0 items-center gap-1.5 rounded-md border border-neutral-800 bg-neutral-950/60 px-2 py-1.5"
          >
            <button
              type="button"
              disabled={!canOpen}
              onClick={() => onOpenTarget(ref.target)}
              className="min-h-11 min-w-0 flex-1 text-left disabled:cursor-default"
              title={canOpen ? `Open ${ref.label}` : ref.label}
            >
              <span className="block truncate text-xs font-semibold text-neutral-200">{ref.label}</span>
              <span className="block truncate text-ui-sm font-medium uppercase text-neutral-500">
                {resourceKindLabel(ref.kind)}
                {ref.status ? ` / ${ref.status}` : ''}
              </span>
            </button>
            {onRemove && (
              <button
                type="button"
                disabled={disabled}
                onClick={() => onRemove(ref)}
                className="flex min-h-11 min-w-11 items-center justify-center rounded-md text-neutral-500 transition-colors hover:bg-neutral-900 hover:text-neutral-200 disabled:opacity-40"
                title={`Stop keeping ${ref.label} in session context`}
                aria-label={`Stop keeping ${ref.label} in session context`}
              >
                <X className="h-3.5 w-3.5" aria-hidden="true" />
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}

function AttentionItem({
  item,
  onClick,
}: {
  item: StrategicStateAttentionItem;
  onClick?: () => void;
}) {
  const content = (
    <>
      <div className="flex min-w-0 items-center justify-between gap-2">
        <span className="truncate text-xs font-medium text-neutral-200">{item.label}</span>
        {item.unread && (
          <span className="shrink-0 text-ui-sm font-semibold uppercase text-amber-300">new</span>
        )}
      </div>
      {item.detail && (
        <p className="mt-0.5 line-clamp-2 text-xs leading-relaxed text-neutral-500">
          {item.detail}
        </p>
      )}
    </>
  );

  if (!onClick) {
    return (
      <div className="min-w-0 border-t border-neutral-800/70 pt-1.5 first:border-t-0 first:pt-0">
        {content}
      </div>
    );
  }

  return (
    <button
      type="button"
      onClick={onClick}
      className="flex min-h-11 w-full min-w-0 flex-col justify-center border-t border-neutral-800/70 py-1.5 text-left transition-colors first:border-t-0 hover:text-neutral-100"
    >
      {content}
    </button>
  );
}

const _PENDING_CONFIRMATION_TYPES: readonly PendingConfirmationType[] = [
  'credentials_needed',
  'access_needed',
  'intent_change',
  'major_decision',
];

/** Map a durable ``kind: 'confirmation'`` attention item onto the
 *  ``PendingConfirmation`` shape consumed by the shared PendingItemRow UI. */
function attentionItemToPendingConfirmation(item: StrategicStateAttentionItem): PendingConfirmation {
  const rawType = item.confirmation_type ?? '';
  const type = (_PENDING_CONFIRMATION_TYPES as readonly string[]).includes(rawType)
    ? (rawType as PendingConfirmationType)
    : 'major_decision';
  return {
    id: item.ref?.id ?? item.id,
    type,
    proposal: item.detail ?? item.label,
    created_at: item.created_at ?? undefined,
  };
}

/** Fully-reachable, inline-actionable list of pending operator confirmations on
 *  the daily surface. Reuses the shared accept/dismiss row and the existing
 *  accept/dismiss API so this is the single place the operator acts on these
 *  items (the problem-model view now renders them read-only). */
function RailPendingConfirmations({
  items,
  onResolved,
  addToast,
}: {
  items: StrategicStateAttentionItem[];
  onResolved?: () => void;
  addToast?: (message: string, type: 'success' | 'error' | 'info') => void;
}) {
  const [resolvedIds, setResolvedIds] = useState<Set<string>>(() => new Set());
  const projectByPendingId = useMemo(() => {
    const map = new Map<string, string>();
    for (const item of items) {
      const pendingId = item.ref?.id ?? item.id;
      const projectId = item.ref?.project_id;
      if (pendingId && projectId) map.set(pendingId, projectId);
    }
    return map;
  }, [items]);
  const pending = useMemo(
    () =>
      items
        .filter(item => !resolvedIds.has(item.ref?.id ?? item.id))
        .map(attentionItemToPendingConfirmation),
    [items, resolvedIds],
  );

  if (pending.length === 0) return null;

  const handleDismiss = async (id: string, note?: string) => {
    const projectId = projectByPendingId.get(id);
    if (!projectId) return;
    try {
      await dismissPendingConfirmation(projectId, id, note);
      setResolvedIds(prev => new Set(prev).add(id));
      onResolved?.();
    } catch {
      addToast?.('Failed to resolve — try again', 'error');
    }
  };

  const handleAccept = async (id: string, answer?: string) => {
    const projectId = projectByPendingId.get(id);
    if (!projectId) return;
    try {
      await acceptPendingConfirmation(projectId, id, answer);
      setResolvedIds(prev => new Set(prev).add(id));
      onResolved?.();
    } catch {
      addToast?.('Failed to accept — try again', 'error');
    }
  };

  return (
    <div className="mt-3 border-t border-neutral-800 pt-3" data-testid="rail-needs-action">
      <div className="mb-1.5 flex min-w-0 items-center justify-between gap-2">
        <span className="truncate text-xs font-semibold text-amber-200">Needs your action</span>
        <span className="shrink-0 font-mono text-ui-sm text-neutral-600">{formatCompactNumber(pending.length)}</span>
      </div>
      <PendingConfirmationList pending={pending} onDismiss={handleDismiss} onAccept={handleAccept} />
    </div>
  );
}

function workSessionActionPriorityText(priority?: string | null): string {
  if (priority === 'critical') return 'text-rose-300';
  if (priority === 'high') return 'text-amber-300';
  if (priority === 'low') return 'text-neutral-600';
  return 'text-neutral-500';
}

function workSessionMessageIntent(action: StrategicWorkSessionAction): StrategicMessageTarget['action'] {
  const targetAction = action.target?.action;
  if (targetAction === 'reply' || targetAction === 'highlight') return targetAction;
  return action.kind === 'reply' ? 'reply' : 'highlight';
}

function messageTargetFromContextTarget(
  target?: StrategicContextPackageDrilldown['target'] | null,
  fallbackAction?: StrategicMessageTarget['action'],
): StrategicMessageTarget | undefined {
  // Mirror StrategicContextWorkspace: only route to the messages surface when
  // the target actually declares it. A 'task' target that happens to carry a
  // message_id must not be misrouted to messages.
  if (target?.surface !== 'messages') return undefined;
  const messageId = target.message_id?.trim() || null;
  if (!messageId) return undefined;
  const action = target.action === 'reply' || target.action === 'highlight'
    ? target.action
    : fallbackAction;
  return action ? { messageId, action } : { messageId };
}

function contextTargetFromWorkSessionTarget(
  target?: StrategicContextPackageDrilldown['target'] | null,
): StrategicContextPackageDrilldown['target'] | null {
  return target?.surface ? target : null;
}

function WorkSessionActionRow({
  action,
  onClick,
}: {
  action: StrategicWorkSessionAction;
  onClick?: () => void;
}) {
  const detail = !action.enabled && action.blocked_reason ? action.blocked_reason : action.reason;
  const showPriority = action.priority === 'critical' || action.priority === 'high';
  const content = (
    <>
      <div className="flex min-w-0 items-center justify-between gap-2">
        <span className="truncate text-xs font-semibold text-neutral-200">{action.label}</span>
        {showPriority && (
          <span className={`shrink-0 text-ui-sm font-semibold uppercase ${workSessionActionPriorityText(action.priority)}`}>
            {action.priority}
          </span>
        )}
      </div>
      {detail && (
        <p className="mt-0.5 line-clamp-2 text-xs leading-relaxed text-neutral-500">
          {detail}
        </p>
      )}
    </>
  );

  if (!onClick) {
    return (
      <div className={`min-w-0 border-t border-neutral-800/70 py-1.5 first:border-t-0 ${action.enabled ? '' : 'opacity-70'}`}>
        {content}
      </div>
    );
  }

  return (
    <button
      type="button"
      onClick={onClick}
      className="flex min-h-11 w-full min-w-0 flex-col justify-center border-t border-neutral-800/70 py-1.5 text-left transition-colors first:border-t-0 hover:text-neutral-100"
    >
      {content}
    </button>
  );
}

const TASK_FOCUS_SECTIONS: Array<{
  key: TaskFocusBucketKey;
  label: string;
  dot: string;
  text: string;
  limit: number;
}> = [
  { key: 'needs_judgment', label: 'Needs judgment', dot: 'bg-amber-300', text: 'text-amber-300', limit: 2 },
  { key: 'active', label: 'Active', dot: 'bg-cyan-400', text: 'text-cyan-300', limit: 2 },
  { key: 'friction', label: 'Friction', dot: 'bg-rose-400', text: 'text-rose-300', limit: 2 },
  { key: 'queued', label: 'Queued', dot: 'bg-neutral-500', text: 'text-neutral-400', limit: 1 },
  { key: 'recent_completed', label: 'Recent result', dot: 'bg-emerald-400', text: 'text-emerald-300', limit: 1 },
];

type TaskFocusSectionConfig = (typeof TASK_FOCUS_SECTIONS)[number];

interface RenderedTaskFocusSection {
  section: TaskFocusSectionConfig;
  bucket: StrategicStateTaskFocusBucket;
  items: StrategicStateTaskFocusItem[];
}

function taskFocusBucket(focus: StrategicStateTaskFocus | null | undefined, key: TaskFocusBucketKey): StrategicStateTaskFocusBucket | null {
  const bucket = focus?.buckets?.[key];
  if (!bucket || bucket.total <= 0) return null;
  return bucket;
}

function buildTaskFocusSections(focus: StrategicStateTaskFocus | null | undefined): RenderedTaskFocusSection[] {
  const renderedIds = new Set<string>();
  let remainingRows = 4;
  const takeItems = (bucket: StrategicStateTaskFocusBucket, limit: number): StrategicStateTaskFocusItem[] => {
    if (remainingRows <= 0) return [];
    const items = (bucket.items ?? [])
      .filter(item => !renderedIds.has(item.id))
      .slice(0, Math.min(limit, remainingRows));
    items.forEach(item => renderedIds.add(item.id));
    remainingRows -= items.length;
    return items;
  };

  const liveSections = TASK_FOCUS_SECTIONS
    .filter(section => section.key !== 'queued' && section.key !== 'recent_completed')
    .map(section => {
      const bucket = taskFocusBucket(focus, section.key);
      if (!bucket) return null;
      const items = takeItems(bucket, section.limit);
      return { section, bucket, items };
    })
    .filter((entry): entry is RenderedTaskFocusSection => Boolean(entry && entry.items.length > 0));

  if (liveSections.length > 0) return liveSections;

  const queuedBucket = taskFocusBucket(focus, 'queued');
  const queuedItems = queuedBucket ? takeItems(queuedBucket, 1) : [];
  if (queuedBucket && queuedItems.length > 0) {
    return [{ section: TASK_FOCUS_SECTIONS.find(section => section.key === 'queued')!, bucket: queuedBucket, items: queuedItems }];
  }

  const completedBucket = taskFocusBucket(focus, 'recent_completed');
  const completedItems = completedBucket ? takeItems(completedBucket, 1) : [];
  if (completedBucket && completedItems.length > 0) {
    return [{ section: TASK_FOCUS_SECTIONS[TASK_FOCUS_SECTIONS.length - 1], bucket: completedBucket, items: completedItems }];
  }

  return [];
}

function taskFocusDetail(item: StrategicStateTaskFocusItem): string {
  const reasons = item.reasons ?? [];
  const movementExplanation = item.movement_explanation?.trim();
  if (movementExplanation) return movementExplanation;
  if (item.detail) return item.detail;
  if (item.latest_status_message) return item.latest_status_message;
  if (reasons.length > 0) return reasons.join(', ');
  return item.status.toLowerCase().replace(/_/g, ' ');
}

function taskFocusMeta(bucket: StrategicStateTaskFocusBucket): string {
  const visibleCount = bucket.items?.length ?? 0;
  if (bucket.has_more && bucket.total > visibleCount) {
    return `${formatCompactNumber(bucket.total)} total`;
  }
  return formatCompactNumber(bucket.total);
}

function TaskFocusRow({
  item,
  onOpenTask,
}: {
  item: StrategicStateTaskFocusItem;
  onOpenTask?: (taskId: string) => void;
}) {
  const detail = taskFocusDetail(item);
  const stepLabel =
    item.bucket === 'active' && item.task_type
      ? item.task_type.replace(/_/g, ' ')
      : null;
  const content = (
    <>
      <div className="flex min-w-0 items-center justify-between gap-2">
        <span className="truncate text-xs font-semibold text-neutral-200">{item.title || item.id}</span>
        <span className="shrink-0 font-mono text-ui-sm text-neutral-600">{shortId(item.id)}</span>
      </div>
      {stepLabel && (
        <p className="mt-0.5 text-xs font-medium text-cyan-400/70 uppercase tracking-wider">
          {stepLabel}
        </p>
      )}
      <p className="mt-0.5 line-clamp-2 text-xs leading-relaxed text-neutral-500">{detail}</p>
    </>
  );

  if (!onOpenTask) {
    return (
      <div className="rounded-md border border-neutral-800 bg-neutral-950/60 px-2.5 py-2">
        {content}
      </div>
    );
  }

  return (
    <button
      type="button"
      onClick={() => onOpenTask(item.id)}
      className="block min-h-11 w-full min-w-0 rounded-md border border-neutral-800 bg-neutral-950/60 px-2.5 py-2 text-left transition-colors hover:border-neutral-700 hover:bg-neutral-900"
    >
      {content}
    </button>
  );
}

function TaskFocusBlock({
  focus,
  onOpenTask,
}: {
  focus?: StrategicStateTaskFocus | null;
  onOpenTask?: (taskId: string) => void;
}) {
  const sections = buildTaskFocusSections(focus);
  const visibleTotal = sections.reduce((sum, entry) => sum + entry.items.length, 0);

  if (sections.length === 0) return null;

  return (
    <WorkbenchPanel title="Task focus" meta={visibleTotal > 0 ? `${formatCompactNumber(visibleTotal)} shown` : 'recent'}>
      <div className="space-y-3" data-testid="strategic-task-focus">
        {sections.slice(0, 4).map(({ section, bucket, items }) => (
          <div key={section.key} className="space-y-1.5">
            <div className="flex min-w-0 items-center justify-between gap-2">
              <div className="flex min-w-0 items-center gap-2">
                <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${section.dot}`} aria-hidden="true" />
                <span className={`truncate text-ui-sm font-semibold uppercase ${section.text}`}>{section.label}</span>
              </div>
              <span className="shrink-0 font-mono text-ui-sm text-neutral-600">{taskFocusMeta(bucket)}</span>
            </div>
            {items.map(item => (
              <TaskFocusRow key={item.id} item={item} onOpenTask={onOpenTask} />
            ))}
          </div>
        ))}
      </div>
    </WorkbenchPanel>
  );
}

function ContextPackageBlock({
  contextPackage,
  sessionRefs,
  savingSessionRefs,
  onOpenTarget,
  onRemoveRef,
}: {
  contextPackage?: StrategicContextPackageDescriptor | null;
  sessionRefs: StrategicWorkSessionRefDescriptor[];
  savingSessionRefs?: boolean;
  onOpenTarget: (target?: StrategicContextPackageDrilldown['target'] | null) => void;
  onRemoveRef?: (ref: Pick<StrategicSelectedRef, 'kind' | 'id'>) => void;
}) {
  if (!contextPackage) return null;
  const tone = contextPackageTone(contextPackage.status);
  const itemCount = Number(contextPackage.item_count ?? 0);
  const nodeCount = contextPackage.canvas?.node_count ?? null;
  const edgeCount = contextPackage.canvas?.edge_count ?? null;
  const descriptorCount = contextPackage.descriptors?.length ?? 0;
  const resourceCount = contextPackage.resources?.length ?? 0;
  const summaryItems = [
    itemCount > 0 ? `${formatCompactNumber(itemCount)} items` : null,
    nodeCount != null
      ? `${formatCompactNumber(nodeCount)} nodes${edgeCount != null ? ` / ${formatCompactNumber(edgeCount)} links` : ''}`
      : null,
    resourceCount > 0 ? `${formatCompactNumber(resourceCount)} refs` : null,
    descriptorCount > 0 ? `${formatCompactNumber(descriptorCount)} views` : null,
  ].filter((item): item is string => Boolean(item));

  return (
    <div className="border-b border-neutral-800 pb-3" data-testid="strategic-context-package-summary">
      <div className="flex min-w-0 gap-2">
        <Network className="mt-0.5 h-4 w-4 shrink-0 text-neutral-500" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">
            <span className="truncate text-xs font-semibold text-neutral-100">Context package</span>
            <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${tone.dot}`} aria-hidden="true" />
            <span className={`shrink-0 text-ui-sm font-semibold uppercase ${tone.text}`}>
              {tone.label}
            </span>
          </div>
          <p className="mt-1 line-clamp-1 text-xs leading-relaxed text-neutral-400">
            {contextPackage.title}
          </p>
          {summaryItems.length > 0 && (
            <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-neutral-500">
              {summaryItems.map(item => <span key={item}>{item}</span>)}
            </div>
          )}
        </div>
      </div>
      <SessionContextList
        refs={sessionRefs}
        onOpenTarget={onOpenTarget}
        onRemove={onRemoveRef}
        disabled={savingSessionRefs}
      />
    </div>
  );
}

export const StrategicContextRail = memo(function StrategicContextRail({
  state,
  loading = false,
  error = null,
  strategistActive = false,
  cycleProgress = null,
  lastCycleCompletion = null,
  onOpenProjectSection,
  onOpenPipeline,
  onOpenRuntimeHealth,
  onOpenRuntimeDevices,
  onOpenRuntimeCompute,
  onOpenAccounts,
  onOpenSpend,
  onOpenContextWorkspace,
  onOpenTask,
  onOpenMessages,
  onOpenKnowledge,
  onOpenTarget,
  workbenchState,
  accountSpend = null,
  savingSessionRefs = false,
  onUpdateSessionRefs,
  onConfirmationResolved,
  addToast,
  projectId = null,
}: StrategicContextRailProps) {
  const activeCycle = state?.strategic.active_cycle ?? null;
  const localRunner = state?.runtime?.local_runner ?? null;
  const runtimeWork = state?.runtime?.work ?? null;
  const pendingSteer = state?.strategic.refresh.pending ?? null;
  const latestCycle = state?.strategic.latest_cycle ?? null;
  const attention = state?.attention ?? null;
  // Pending operator confirmations get a dedicated, fully-reachable actionable
  // list below; the generic attention preview stays capped and excludes them so
  // they are never truncated away or shown twice.
  const confirmationItems = useMemo(
    () => attention?.items.filter(item => item.kind === 'confirmation') ?? [],
    [attention?.items],
  );
  const attentionItems = useMemo(
    () => attention?.items.filter(item => item.kind !== 'confirmation').slice(0, 2) ?? [],
    [attention?.items],
  );
  const staleCycle = Boolean(activeCycle?.stale);
  const shaFreshness = state?.sha_freshness ?? null;
  const operatorSteerQueued = Boolean(
    pendingSteer && (pendingSteer.origin === 'operator' || (pendingSteer.operator_count ?? 0) > 0),
  );
  const isWorking = !staleCycle && (strategistActive || Boolean(activeCycle?.has_active_task));
  const statusTone = isWorking ? 'bg-cyan-400' : pendingSteer ? 'bg-amber-300' : 'bg-neutral-600';
  const formatTimestampAsOf = (timestamp?: string | null): string => {
    if (!timestamp) return '';
    try {
      const date = new Date(timestamp);
      if (Number.isNaN(date.getTime())) return '';
      const now = new Date();
      const diffMs = now.getTime() - date.getTime();
      const diffMins = Math.floor(diffMs / 60000);
      const diffHours = Math.floor(diffMins / 60);
      const diffDays = Math.floor(diffHours / 24);

      if (diffMins < 1) return 'just now';
      if (diffMins < 60) return `${diffMins}m ago`;
      if (diffHours < 24) return `${diffHours}h ago`;
      if (diffDays < 7) return `${diffDays}d ago`;
      return date.toLocaleDateString();
    } catch {
      return '';
    }
  };

  const staleCycleLabel = ((): string => {
    if (!staleCycle) return '';
    const asOfTime = formatTimestampAsOf(shaFreshness?.generated_at);
    const asOfSuffix = asOfTime ? ` · ${asOfTime}` : '';

    if (shaFreshness?.verdict === 'stale') {
      const cSha = shaFreshness.active_candidate_sha?.slice(0, 7);
      const iSha = shaFreshness.integration_head_sha?.slice(0, 7);
      if (cSha && iSha) return `Cycle stale · candidate @${cSha} vs integration @${iSha}${asOfSuffix}`;
      if (cSha) return `Cycle stale · candidate @${cSha}${asOfSuffix}`;
      if (iSha) return `Cycle stale · integration @${iSha}${asOfSuffix}`;
    }
    if (shaFreshness?.verdict === 'unknown' && asOfTime) {
      return `Cycle freshness unknown${asOfSuffix}`;
    }
    return 'Cycle stale';
  })();
  const statusLabel = isWorking
    ? (cycleProgress?.phase_label ?? phaseLabel(state?.strategic.active_phase))
    : staleCycle
      ? staleCycleLabel
    : pendingSteer
      ? operatorSteerQueued
        ? 'Steer queued'
        : 'Refresh queued'
      : 'Idle';
  const statusDetail = isWorking
    ? (state?.strategic.active_task?.title ?? state?.strategic.active_task?.task_type ?? 'Cycle running')
    : staleCycle
      ? 'A prior Strategic cycle still has an active marker, but no live Strategic task is attached.'
    : pendingSteer?.excerpt
      ? pendingSteer.excerpt
      : latestCycle?.summary_text ?? lastCycleCompletion?.status_message ?? 'No active Strategic cycle';
  const localRunnerStatus = localRunner ? localRunnerTone(localRunner) : null;
  const showRuntimeWork = runtimeWorkHasSignal(runtimeWork);
  const runtimeWorkStatus = showRuntimeWork ? runtimeWorkTone(runtimeWork) : null;
  const firstRuntimeJob = runtimeWork?.compute_jobs.items[0] ?? null;
  const firstLocalRunnerHold = runtimeWork?.local_runner_holds?.items[0] ?? null;
  const hasLocalRunnerHolds = (runtimeWork?.local_runner_holds?.count ?? 0) > 0;
  const accountStatus = accountSpend ? accountSpendTone(accountSpend) : null;
  const accountDetail = accountSpend ? accountSpendDetail(accountSpend) : null;
  const showAccountStatus = Boolean(accountSpend && accountStatus && accountSpendHasProblem(accountSpend, accountStatus));
  const openAccountSpend = onOpenAccounts ?? onOpenSpend;
  const accountActionText = onOpenAccounts ? 'Accounts' : 'Spend';
  const accountActionLabel = onOpenAccounts ? 'accounts' : 'spend';
  const workSessionActions = state?.work_session?.actions ?? [];
  const visibleWorkSessionActions = useMemo(
    () => workSessionActions.filter(action => action.enabled).slice(0, 4),
    [workSessionActions],
  );
  const visibleWorkSessionActionIds = useMemo(
    () => new Set(visibleWorkSessionActions.map(action => action.id)),
    [visibleWorkSessionActions],
  );
  const visibleAttentionItems = useMemo(
    () => attentionItems.filter(item => !visibleWorkSessionActionIds.has(`attention.${item.id}`)),
    [attentionItems, visibleWorkSessionActionIds],
  );
  const actionableWorkSessionEvents = useMemo(
    () => (state?.work_session?.events?.items ?? []).filter(workSessionEventHasActionableSignal).slice(0, 2),
    [state?.work_session?.events?.items],
  );
  const showActionableSessionActivity = visibleWorkSessionActions.length === 0 && actionableWorkSessionEvents.length > 0;
  const selectedRefs = workbenchState?.selected_refs ?? state?.work_session?.selected_refs ?? state?.workbench?.selected_refs ?? [];
  const workSessionDescriptors = state?.work_session?.selected_ref_descriptors ?? [];
  const selectedRefDescriptors = useMemo(() => {
    const descriptorByRef = new Map(workSessionDescriptors.map(descriptor => [sessionRefKey(descriptor), descriptor]));
    return selectedRefs.map(ref => descriptorByRef.get(sessionRefKey(ref)) ?? fallbackRefDescriptor(ref));
  }, [workSessionDescriptors, selectedRefs]);

  function attentionItemAction(item: StrategicStateAttentionItem): (() => void) | undefined {
    if (item.target?.surface === 'task') {
      const taskId = item.target.task_id ?? item.task_id ?? item.ref?.id;
      return onOpenTask && taskId ? () => onOpenTask(taskId) : onOpenPipeline;
    }
    if (item.target) return () => openContextTarget(item.target);
    if (item.kind === 'task') {
      return onOpenTask && item.ref?.id ? () => onOpenTask(item.ref.id) : onOpenPipeline;
    }
    if (item.kind === 'direction') return () => onOpenProjectSection('directions');
    if (item.kind === 'alert' && item.task_id) {
      const taskId = item.task_id;
      return onOpenTask ? () => onOpenTask(taskId) : onOpenPipeline;
    }
    if (item.kind === 'alert') return onOpenRuntimeHealth;
    if (item.kind === 'message') {
      if (!onOpenMessages) return onOpenPipeline;
      const messageId = item.ref?.id;
      return () => onOpenMessages(messageId ? {
        messageId,
        action: item.reason === 'question' ? 'reply' : 'highlight',
      } : undefined);
    }
    return undefined;
  }

  function fallbackOpenContextTarget(target?: StrategicContextPackageDrilldown['target'] | null): void {
    if (!target) return;
    if (target.surface === 'project') {
      if (isProjectTarget(target.section)) onOpenProjectSection(target.section);
      return;
    }
    if (target.surface === 'pipeline') {
      onOpenPipeline();
      return;
    }
    if (target.surface === 'runtime') {
      if (target.section === 'devices' && onOpenRuntimeDevices) onOpenRuntimeDevices();
      else if (target.section === 'compute' && onOpenRuntimeCompute) onOpenRuntimeCompute();
      else onOpenRuntimeHealth();
      return;
    }
    if (target.surface === 'messages') {
      if (onOpenMessages) onOpenMessages(messageTargetFromContextTarget(target));
      else onOpenPipeline();
      return;
    }
    if (target.surface === 'task') {
      if (target.task_id && onOpenTask) onOpenTask(target.task_id);
      else onOpenPipeline();
      return;
    }
    if (target.surface === 'knowledge') {
      if (onOpenKnowledge) onOpenKnowledge(target.document_id, target.direction_id);
      else onOpenProjectSection('directions');
    }
  }

  function openContextTarget(target?: StrategicContextPackageDrilldown['target'] | null): void {
    if (!target) return;
    (onOpenTarget ?? fallbackOpenContextTarget)(target);
  }

  function workSessionActionClick(action: StrategicWorkSessionAction): (() => void) | undefined {
    if (!action.enabled) return undefined;
    const target = action.target ?? null;
    const messageTarget = messageTargetFromContextTarget(target, workSessionMessageIntent(action));
    if (messageTarget || target?.surface === 'messages') {
      if (!onOpenMessages) return onOpenPipeline;
      return () => onOpenMessages(messageTarget);
    }

    const contextTarget = contextTargetFromWorkSessionTarget(target);
    if (
      contextTarget
      && target?.surface
      && target.surface !== 'task'
    ) {
      return () => openContextTarget(contextTarget);
    }

    const taskId = target?.task_id ?? null;
    if (target?.surface === 'task' || (!target?.surface && taskId)) {
      return taskId && onOpenTask ? () => onOpenTask(taskId) : onOpenPipeline;
    }

    return contextTarget ? () => openContextTarget(contextTarget) : undefined;
  }

  function removeSessionRef(ref: Pick<StrategicSelectedRef, 'kind' | 'id'>): void {
    if (!onUpdateSessionRefs) return;
    onUpdateSessionRefs(refs => removeStrategicSelectedRef(refs, ref));
  }

  return (
    <div className="space-y-3" data-testid="strategic-context-rail">
      <WorkbenchPanel title="Strategic" meta={loading ? 'loading' : error ? 'stale' : 'live'}>
        <div className="flex min-w-0 items-start gap-3">
          <span className={`mt-1 h-2 w-2 shrink-0 rounded-full ${statusTone}`} aria-hidden="true" />
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold text-neutral-100">{statusLabel}</div>
            <p className="mt-1 line-clamp-3 text-xs leading-relaxed text-neutral-400">{statusDetail}</p>
          </div>
        </div>
        {pendingSteer?.operator_count ? (
          <div className="mt-3 border-t border-neutral-800 pt-2">
            <StatRow label="queued steers" value={pendingSteer.operator_count} />
          </div>
        ) : null}
        <div className="mt-3 flex flex-wrap gap-2 border-t border-neutral-800 pt-3">
          {onOpenContextWorkspace && <RailButton onClick={onOpenContextWorkspace}>Open context</RailButton>}
          <RailButton onClick={onOpenPipeline}>Pipeline</RailButton>
        </div>
        {localRunner && localRunnerStatus && (
          <div className="mt-3 border-t border-neutral-800 pt-3">
            <div className="flex min-w-0 items-center gap-2">
              <Cpu className="h-4 w-4 shrink-0 text-neutral-500" aria-hidden="true" />
              <div className="min-w-0 flex-1">
                <div className="flex min-w-0 items-center gap-2">
                  <span className="truncate text-xs font-semibold text-neutral-100">Local Strategic</span>
                  <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${localRunnerStatus.dot}`} aria-hidden="true" />
                  <span className={`shrink-0 text-ui-sm font-semibold uppercase ${localRunnerStatus.text}`}>
                    {localRunnerStatus.label}
                  </span>
                </div>
                <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-neutral-500">
                  {localRunnerDetail(localRunner)}
                </p>
              </div>
              <RailButton onClick={onOpenRuntimeDevices ?? onOpenRuntimeHealth}>Devices</RailButton>
            </div>
          </div>
        )}
        {accountSpend && accountStatus && showAccountStatus && (
          <div className="mt-3 border-t border-neutral-800 pt-3" data-testid="strategic-account-status">
            <div className="flex min-w-0 items-center gap-2">
              <CreditCard className="h-4 w-4 shrink-0 text-neutral-500" aria-hidden="true" />
              <div className="min-w-0 flex-1">
                <div className="flex min-w-0 items-center gap-2">
                  <span className="truncate text-xs font-semibold text-neutral-100">Accounts</span>
                  <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${accountStatus.dot}`} aria-hidden="true" />
                  <span className={`shrink-0 text-ui-sm font-semibold uppercase ${accountStatus.text}`}>
                    {accountStatus.label}
                  </span>
                </div>
                <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-neutral-500">
                  {accountDetail}
                </p>
              </div>
              {openAccountSpend && (
                <RailButton
                  onClick={openAccountSpend}
                  ariaLabel={`Open ${accountActionLabel}: ${accountStatus.label}${accountDetail ? `, ${accountDetail}` : ''}`}
                >
                  {accountActionText}
                </RailButton>
              )}
            </div>
          </div>
        )}
        {showRuntimeWork && runtimeWorkStatus && (
          <div className="mt-3 border-t border-neutral-800 pt-3">
            <div className="flex min-w-0 items-center gap-2">
              <Activity className="h-4 w-4 shrink-0 text-neutral-500" aria-hidden="true" />
              <div className="min-w-0 flex-1">
                <div className="flex min-w-0 items-center gap-2">
                  <span className="truncate text-xs font-semibold text-neutral-100">Runtime work</span>
                  <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${runtimeWorkStatus.dot}`} aria-hidden="true" />
                  <span className={`shrink-0 text-ui-sm font-semibold uppercase ${runtimeWorkStatus.text}`}>
                    {runtimeWorkStatus.label}
                  </span>
                </div>
                <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-neutral-500">
                  {runtimeWorkDetail(runtimeWork)}
                </p>
                {(firstLocalRunnerHold?.title || firstRuntimeJob?.task_title) && (
                  <p className="mt-1 truncate text-ui-sm text-neutral-600">
                    {firstLocalRunnerHold?.title ?? firstRuntimeJob?.task_title}
                  </p>
                )}
              </div>
              <RailButton
                onClick={
                  hasLocalRunnerHolds
                    ? (onOpenRuntimeDevices ?? onOpenRuntimeHealth)
                    : (onOpenRuntimeCompute ?? onOpenRuntimeHealth)
                }
              >
                {hasLocalRunnerHolds ? 'Devices' : 'Compute'}
              </RailButton>
            </div>
          </div>
        )}
        {confirmationItems.length > 0 && (
          <RailPendingConfirmations
            items={confirmationItems}
            onResolved={onConfirmationResolved}
            addToast={addToast}
          />
        )}
        {attention && attention.level !== 'clear' && (
          <div className="mt-3 border-t border-neutral-800 pt-3">
            <div className="flex min-w-0 gap-2">
              <CircleAlert
                className={`mt-0.5 h-4 w-4 shrink-0 ${
                  attention.level === 'needs_operator' ? 'text-amber-300' : 'text-neutral-500'
                }`}
                aria-hidden="true"
              />
              <div className="min-w-0">
                <div className="text-xs font-semibold text-neutral-100">
                  {attention.level === 'needs_operator' ? 'Needs operator' : 'Watch'}
                </div>
                <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-neutral-400">
                  {attention.summary}
                </p>
              </div>
            </div>
            {visibleAttentionItems.length > 0 && (
              <div className="mt-2 space-y-1.5">
                {visibleAttentionItems.map(item => (
                  <AttentionItem key={item.id} item={item} onClick={attentionItemAction(item)} />
                ))}
              </div>
            )}
          </div>
        )}
        {visibleWorkSessionActions.length > 0 && (
          <div className="mt-3 border-t border-neutral-800 pt-3" data-testid="strategic-next-actions">
            <div className="mb-1 flex min-w-0 items-center justify-between gap-2">
              <span className="truncate text-xs font-semibold text-neutral-100">Next actions</span>
              <span className="shrink-0 font-mono text-ui-sm text-neutral-600">
                {formatCompactNumber(workSessionActions.length)}
              </span>
            </div>
            <div className="space-y-1">
              {visibleWorkSessionActions.map(action => (
                <WorkSessionActionRow
                  key={action.id}
                  action={action}
                  onClick={workSessionActionClick(action)}
                />
              ))}
            </div>
          </div>
        )}
        {showActionableSessionActivity && (
          <div className="mt-3 border-t border-neutral-800 pt-3" data-testid="strategic-session-activity">
            <div className="mb-1 flex min-w-0 items-center justify-between gap-2">
              <span className="truncate text-xs font-semibold text-neutral-100">Recent signal</span>
              <span className="shrink-0 font-mono text-ui-sm text-neutral-600">
                {formatCompactNumber(actionableWorkSessionEvents.length)}
              </span>
            </div>
            <StrategicSessionEvents
              events={actionableWorkSessionEvents}
              limit={2}
              density="compact"
              onOpenTarget={openContextTarget}
            />
          </div>
        )}
        <ShaFreshnessBlock shaFreshness={shaFreshness} />
      </WorkbenchPanel>

      <WhiteboardRailSection projectId={projectId} />

      <TaskFocusBlock
        focus={state?.tasks.focus ?? null}
        onOpenTask={onOpenTask}
      />

      <WorkbenchPanel title="Context" meta={formatCost(state?.tasks.cumulative_estimated_cost_usd)}>
        <div className="space-y-3">
          <div className="space-y-3 border-b border-neutral-800 pb-3">
            <div data-testid="strategic-problem-summary">
              <div className="flex min-w-0 gap-2">
                <Radio className="mt-0.5 h-4 w-4 shrink-0 text-neutral-500" aria-hidden="true" />
                <div className="min-w-0 flex-1">
                  <div className="flex min-w-0 items-center justify-between gap-2">
                    <span className="truncate text-xs font-semibold text-neutral-100">Problem</span>
                    {state?.problem_model.revision && (
                      <span className="shrink-0 font-mono text-ui-sm text-neutral-600">r{state.problem_model.revision}</span>
                    )}
                  </div>
                  <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-neutral-300">
                    {state?.problem_model.current_focus || state?.problem_model.purpose || 'No problem model focus yet.'}
                  </p>
                  {state?.problem_model.top_gap && (
                    <p className="mt-1 line-clamp-1 text-ui-sm text-neutral-500">
                      {state.problem_model.top_gap.name}
                    </p>
                  )}
                </div>
              </div>
              <div className="mt-2 flex flex-wrap gap-2 pl-6">
                <RailButton onClick={() => onOpenProjectSection('problem-model')}>Model</RailButton>
              </div>
            </div>

            <div className="border-t border-neutral-800 pt-3" data-testid="strategic-direction-summary">
              <div className="flex min-w-0 gap-2">
                <GitBranch className="mt-0.5 h-4 w-4 shrink-0 text-neutral-500" aria-hidden="true" />
                <div className="min-w-0 flex-1">
                  <div className="flex min-w-0 items-center justify-between gap-2">
                    <span className="truncate text-xs font-semibold text-neutral-100">Direction</span>
                    <span className="shrink-0 font-mono text-ui-sm text-neutral-600">
                      {state?.directions.active_count ?? 0} active
                    </span>
                  </div>
                  <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-neutral-300">
                    {state?.directions.primary?.text || 'No active direction selected.'}
                  </p>
                </div>
              </div>
              <div className="mt-2 flex flex-wrap gap-2 pl-6">
                <RailButton onClick={() => onOpenProjectSection('directions')}>Directions</RailButton>
                <RailButton onClick={() => onOpenProjectSection('running-update')}>Update</RailButton>
              </div>
            </div>
          </div>
          <RailMiniCanvas
            canvas={state?.context_package?.canvas}
            strategistActive={isWorking}
            activeTaskLabel={state?.strategic.active_task?.title ?? null}
            activeDirectionLabel={state?.directions.primary?.text ?? null}
          />
          <ContextPackageBlock
            contextPackage={state?.context_package}
            sessionRefs={selectedRefDescriptors}
            savingSessionRefs={savingSessionRefs}
            onOpenTarget={openContextTarget}
            onRemoveRef={onUpdateSessionRefs ? removeSessionRef : undefined}
          />
          <div className="flex flex-wrap gap-2">
            <RailButton onClick={onOpenRuntimeHealth}>Runtime</RailButton>
            {openAccountSpend && !showAccountStatus && (
              <RailButton
                onClick={openAccountSpend}
                ariaLabel={`Open ${accountActionLabel}`}
              >
                {accountActionText}
              </RailButton>
            )}
            {state?.design.has_design_doc && (
              <RailButton onClick={() => onOpenProjectSection('design-doc')}>Design</RailButton>
            )}
          </div>
        </div>
      </WorkbenchPanel>
    </div>
  );
});
