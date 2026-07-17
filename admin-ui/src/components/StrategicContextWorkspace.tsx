import { useEffect, useMemo, type ReactNode } from 'react';
import { Activity, ArrowUpRight, CircleAlert, Cpu, GitBranch, Link2, Network, Plus, Server, X } from 'lucide-react';
import type {
  StrategicContextArtifactEvidence,
  StrategicContextPackageDescriptor,
  StrategicContextCanvasNode,
  StrategicContextPackageDrilldown,
  StrategicContextResourceRef,
  StrategicMessageTarget,
  StrategicLocalRunnerState,
  StrategicRuntimeWorkState,
  StrategicStateResponse,
  StrategicWorkbenchState,
  StrategicWorkSessionAction,
  StrategicWorkSessionMetric,
  StrategicWorkSessionRefDescriptor,
} from '../strategicStateApi';
import { formatStrategicMetricValue, rankStrategicWorkSessionMetrics } from '../utils/strategicWorkSessionMetrics';
import {
  MAX_STRATEGIC_SELECTED_REFS,
  normalizeStrategicInteractionMode,
  removeStrategicSelectedRef,
  upsertStrategicSelectedRef,
  type StrategicSelectedRef,
} from '../utils/strategicWorkbenchSession';
import { StrategicContextMap } from './StrategicContextMap';
import { ArtifactViewablePreviews, StrategicContextArtifactSlot } from './StrategicContextArtifactSlot';
import { StrategicSessionEvents } from './StrategicSessionEvents';
import {
  StrategicSessionControl,
  type StrategicAccountSpendStatus,
  type StrategicInteractionMode,
  type StrategicResidentRunnerOption,
} from './StrategicSessionControl';

type StrategicContextTarget = StrategicContextPackageDrilldown['target'];
type StrategicContextCanvas = NonNullable<StrategicContextPackageDescriptor['canvas']>;
const CONTEXT_LIST_CLASS = 'divide-y divide-neutral-800/70 border-y border-neutral-800/70';

interface StrategicContextWorkspaceProps {
  open: boolean;
  state: StrategicStateResponse | null;
  workbenchState?: StrategicWorkbenchState | null;
  savingSession?: boolean;
  savingSessionRefs?: boolean;
  accountSpend?: StrategicAccountSpendStatus | null;
  sessionActive?: boolean;
  sessionStatus?: string | null;
  sessionState?: string | null;
  sessionReason?: string | null;
  sessionBusy?: boolean;
  sessionRunners?: StrategicResidentRunnerOption[] | null;
  sessionSelectedRunnerId?: string | null;
  operatorDecisionSummary?: {
    total: number;
    captured: number;
    missed: number;
    capture_rate: number | null;
  } | null;
  projectId?: string | null;
  onSelectRunner?: (runnerId: string | null) => void;
  onStartSession?: () => void;
  onStopSession?: () => void;
  onClose: () => void;
  onOpenTarget: (target?: StrategicContextTarget | null) => void;
  onOpenMessageTarget?: (target?: StrategicMessageTarget) => void;
  onModeChange?: (mode: StrategicInteractionMode) => void;
  onOpenAccounts?: () => void;
  onOpenSpend?: () => void;
  onUpdateSessionRefs?: (
    updateRefs: (refs: StrategicWorkbenchState['selected_refs']) => StrategicWorkbenchState['selected_refs']
  ) => void;
}

type OpenContextTarget = (
  target?: StrategicContextTarget | null,
  messageAction?: StrategicMessageTarget['action'],
) => void;

function formatCompactNumber(value: number | null | undefined): string {
  const n = Number(value ?? 0);
  if (!Number.isFinite(n)) return '0';
  if (Math.abs(n) >= 1000) return `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}k`;
  return String(n);
}

function contextStatusTone(status?: string | null): { dot: string; text: string; label: string } {
  if (status === 'building') return { dot: 'bg-cyan-400', text: 'text-cyan-300', label: 'building' };
  if (status === 'stale') return { dot: 'bg-amber-300', text: 'text-amber-300', label: 'stale' };
  if (!status) return { dot: 'bg-neutral-600', text: 'text-neutral-500', label: 'empty' };
  return { dot: 'bg-emerald-400', text: 'text-emerald-300', label: 'ready' };
}

function contextKindLabel(kind?: string | null): string {
  if (kind === 'canvas') return 'context map';
  if (kind === 'knowledge_document') return 'knowledge';
  if (kind === 'artifact') return 'artifact';
  if (kind === 'task') return 'task';
  return (kind || 'context').replace(/_/g, ' ');
}

function contextRefKey(ref: Pick<StrategicSelectedRef, 'kind' | 'id'>): string {
  return `${ref.kind}:${ref.id}`;
}

function selectedRefFromResource(resource: StrategicContextResourceRef): StrategicSelectedRef {
  return {
    kind: resource.kind,
    id: resource.id,
    label: resource.label,
  };
}

function selectedRefFromCanvas(canvas: StrategicContextCanvas): StrategicSelectedRef | null {
  if (!canvas.id) return null;
  return {
    kind: 'canvas',
    id: canvas.id,
    label: canvas.title ?? 'Strategic context map',
  };
}

function selectedRefFromNode(node: StrategicContextCanvasNode): StrategicSelectedRef {
  return {
    kind: node.kind,
    id: node.id,
    label: node.label,
  };
}

function selectedRefFromKnowledgeEvidence(item: {
  id: string;
  label?: string | null;
  topic?: string | null;
}): StrategicSelectedRef {
  return {
    kind: 'knowledge_document',
    id: item.id.startsWith('knowledge:') ? item.id : `knowledge:${item.id}`,
    label: item.label ?? item.topic ?? 'Knowledge document',
  };
}

function selectedRefFromArtifactEvidence(item: StrategicContextArtifactEvidence): StrategicSelectedRef {
  return {
    kind: 'artifact',
    id: item.id.startsWith('artifact:') ? item.id : `artifact:${item.id}`,
    label: item.label ?? item.logical_key ?? item.artifact_type ?? 'Artifact',
  };
}

function fallbackRefDescriptor(ref: StrategicSelectedRef): StrategicWorkSessionRefDescriptor {
  return {
    kind: ref.kind,
    id: ref.id,
    label: ref.label || `${contextKindLabel(ref.kind)} ${ref.id.slice(0, 8)}`,
  };
}

function targetLabel(target?: StrategicContextTarget | null): string | null {
  if (!target) return null;
  const parts = [target.surface, target.section, target.task_id, target.document_id, target.artifact_id]
    .filter((part): part is string => typeof part === 'string' && part.trim().length > 0);
  return parts.join(' / ');
}

function pluralize(count: number, singular: string): string {
  return `${count} ${singular}${count === 1 ? '' : 's'}`;
}

function shortId(value?: string | null): string | null {
  return value ? value.slice(0, 8) : null;
}

function compactDetail(...parts: Array<string | null | undefined>): string | null {
  const detailParts = parts.map(part => part?.trim()).filter((part): part is string => Boolean(part));
  return detailParts.length > 0 ? detailParts.join(' / ') : null;
}

function canvasSummaryDetail(
  canvas: StrategicContextCanvas | null,
  nodeCount: number,
  edgeCount: number,
): string {
  const nodes = canvas?.node_count ?? nodeCount;
  const edges = canvas?.edge_count ?? edgeCount;
  return `${formatCompactNumber(nodes)} nodes / ${formatCompactNumber(edges)} links. Keep this map to carry the whole discussion frame into the session.`;
}

function sessionModeLabel(mode?: string | null): string {
  if (mode === 'check_in') return 'Check-in';
  if (mode === 'monitor') return 'Monitor';
  if (mode === 'continuous') return 'Workday';
  return 'Chat';
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
  if (localRunner.eligible_live_count > 0) return `${pluralize(localRunner.eligible_live_count, 'runner')} ready`;
  if (localRunner.fallback_reason === 'no_runner') return 'No local Strategic runner enrolled';
  if (localRunner.fallback_reason === 'runner_offline') return `${pluralize(localRunner.strategic_runner_count, 'runner')} enrolled, none live`;
  if (localRunner.fallback_reason === 'agent_unavailable') return 'Runner live, agent unavailable';
  if (localRunner.fallback_reason === 'at_capacity') return 'Runner busy with active work';
  if (localRunner.fallback_reason === 'cloud_dispatch') return 'Current cycle already dispatched outside the runner';
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
  if ((work.compute_jobs?.counts.terminal_recent ?? 0) > 0) {
    return { dot: 'bg-neutral-500', text: 'text-neutral-400', label: 'recent result' };
  }
  return { dot: 'bg-neutral-600', text: 'text-neutral-400', label: 'quiet' };
}

function runtimeWorkDetail(work: StrategicRuntimeWorkState): string {
  const parts = [
    (work.compute_jobs?.counts.active ?? 0) > 0 ? pluralize(work.compute_jobs.counts.active, 'compute job') : null,
    (work.compute_jobs?.counts.queued ?? 0) > 0 ? `${formatCompactNumber(work.compute_jobs.counts.queued)} queued` : null,
    (work.local_runner_holds?.count ?? 0) > 0 ? pluralize(work.local_runner_holds?.count ?? 0, 'local runner hold') : null,
    (work.held_approvals?.count ?? 0) > 0 ? pluralize(work.held_approvals.count, 'approval') : null,
    (work.parked_wakes?.counts.active ?? 0) > 0 ? pluralize(work.parked_wakes.counts.active, 'parked wake') : null,
    (work.compute_jobs?.counts.failed_recent ?? 0) > 0 ? pluralize(work.compute_jobs.counts.failed_recent, 'recent failure') : null,
    (work.compute_jobs?.counts.timed_out_recent ?? 0) > 0 ? pluralize(work.compute_jobs.counts.timed_out_recent, 'timeout') : null,
  ].filter(Boolean);
  if (parts.length > 0) return parts.join(' / ');
  const terminal = work.compute_jobs?.counts.terminal_recent ?? 0;
  return terminal > 0 ? pluralize(terminal, 'recent compute result') : 'No active background work';
}

function metricToneClass(tone?: string | null): string {
  if (tone === 'critical') return 'text-rose-300';
  if (tone === 'warning') return 'text-amber-300';
  if (tone === 'active') return 'text-cyan-300';
  if (tone === 'watch') return 'text-neutral-200';
  return 'text-neutral-100';
}

function workSessionActionDetail(action: StrategicWorkSessionAction): string | null {
  return compactDetail(!action.enabled ? action.blocked_reason : action.reason, action.priority === 'normal' ? null : action.priority);
}

function normalizeActionTarget(action: StrategicWorkSessionAction): StrategicContextTarget | null {
  const target = action.target ?? null;
  if (!target) return null;
  if (target.surface) return target;
  if (target.task_id) return { ...target, surface: 'task' };
  if (target.message_id) return { ...target, surface: 'messages' };
  return null;
}

function messageTargetFromContextTarget(
  target?: StrategicContextTarget | null,
  fallbackAction?: StrategicMessageTarget['action'],
): StrategicMessageTarget | null {
  if (target?.surface !== 'messages') return null;
  const messageId = target.message_id?.trim();
  if (!messageId) return null;
  const action = target.action === 'reply' || target.action === 'highlight'
    ? target.action
    : fallbackAction;
  return action ? { messageId, action } : { messageId };
}

function Section({
  title,
  meta,
  children,
  testId,
}: {
  title: string;
  meta?: string | null;
  children: ReactNode;
  testId?: string;
}) {
  return (
    <section className="min-w-0 overflow-hidden rounded-md border border-workbench-border bg-workbench-layer" data-testid={testId}>
      <div className="flex min-w-0 items-center justify-between gap-3 border-b border-neutral-800 px-3 py-2.5">
        <h3 className="truncate text-sm font-semibold text-neutral-100">{title}</h3>
        {meta && <span className="shrink-0 text-xs text-neutral-500">{meta}</span>}
      </div>
      <div className="min-w-0 p-2.5">{children}</div>
    </section>
  );
}

function EmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-md border border-dashed border-neutral-800 px-3 py-4 text-sm text-neutral-500">
      {children}
    </div>
  );
}

function SessionRefAction({
  selected,
  disabled,
  label,
  onKeep,
  onRemove,
}: {
  selected: boolean;
  disabled?: boolean;
  label: string;
  onKeep?: () => void;
  onRemove?: () => void;
}) {
  if (!onKeep && !onRemove) return null;
  if (selected) {
    return (
      <button
        type="button"
        disabled={disabled}
        onClick={onRemove}
        className="inline-flex min-h-11 shrink-0 items-center gap-1 rounded-md border border-neutral-800 px-2 text-ui-sm font-semibold text-neutral-400 transition-colors hover:border-neutral-700 hover:bg-neutral-900 hover:text-neutral-100 disabled:opacity-40"
        title={`Remove ${label} from session context`}
        aria-label={`Remove ${label} from session context`}
      >
        <X className="h-3.5 w-3.5" aria-hidden="true" />
        <span>Remove</span>
      </button>
    );
  }
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onKeep}
      className="inline-flex min-h-11 shrink-0 items-center gap-1 rounded-md border border-neutral-800 px-2 text-ui-sm font-semibold text-neutral-400 transition-colors hover:border-neutral-700 hover:bg-neutral-900 hover:text-neutral-100 disabled:opacity-40"
      title={`Keep ${label} in session context`}
      aria-label={`Keep ${label} in session context`}
    >
      <Plus className="h-3.5 w-3.5" aria-hidden="true" />
      <span>Keep</span>
    </button>
  );
}

function ContextRow({
  label,
  kind,
  detail,
  target,
  selected,
  saving,
  badge,
  onOpenTarget,
  onKeep,
  onRemove,
}: {
  label: string;
  kind: string;
  detail?: string | null;
  target?: StrategicContextTarget | null;
  selected?: boolean;
  saving?: boolean;
  badge?: ReactNode;
  onOpenTarget: OpenContextTarget;
  onKeep?: () => void;
  onRemove?: () => void;
}) {
  const canOpen = Boolean(target);
  const bodyClassName = 'min-h-11 min-w-0 flex-1 text-left';
  const content = (
    <>
      <span className="flex min-w-0 items-center gap-2">
        <span className="truncate text-sm font-semibold text-neutral-100">{label}</span>
        {badge}
      </span>
      <span className="mt-0.5 block truncate text-ui-sm font-semibold uppercase text-neutral-500">
        {contextKindLabel(kind)}
        {targetLabel(target) ? ` / ${targetLabel(target)}` : ''}
      </span>
      {detail && <span className="mt-0.5 block line-clamp-2 text-xs leading-relaxed text-neutral-500">{detail}</span>}
    </>
  );
  return (
    <div className="flex min-w-0 items-center gap-2 border-t border-neutral-800/70 py-2 first:border-t-0">
      {canOpen ? (
        <button
          type="button"
          onClick={() => onOpenTarget(target)}
          className={bodyClassName}
          title={`Open ${label}`}
        >
          {content}
        </button>
      ) : (
        <div className={bodyClassName} title={label}>
          {content}
        </div>
      )}
      {canOpen && <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-neutral-600" aria-hidden="true" />}
      <SessionRefAction
        selected={Boolean(selected)}
        disabled={saving}
        label={label}
        onKeep={onKeep}
        onRemove={onRemove}
      />
    </div>
  );
}

function EdgeRow({
  source,
  target,
  label,
}: {
  source: string;
  target: string;
  label?: string | null;
}) {
  return (
    <div className="flex min-w-0 items-center gap-2 border-t border-neutral-800/70 py-2 first:border-t-0">
      <Link2 className="h-3.5 w-3.5 shrink-0 text-neutral-600" aria-hidden="true" />
      <div className="min-w-0">
        <div className="truncate text-sm font-semibold text-neutral-200">
          {source} <span className="text-neutral-600">to</span> {target}
        </div>
        {label && <div className="mt-0.5 truncate text-xs text-neutral-500">{label}</div>}
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status?: string | null }) {
  if (!status) return null;
  return (
    <span className="shrink-0 rounded border border-neutral-800 px-1 text-ui-sm font-semibold uppercase text-neutral-500">
      {status}
    </span>
  );
}

function SessionSummaryButton({
  icon,
  label,
  value,
  detail,
  tone,
  target,
  onOpenTarget,
}: {
  icon: ReactNode;
  label: string;
  value: string;
  detail?: string | null;
  tone?: { dot: string; text: string };
  target?: StrategicContextTarget | null;
  onOpenTarget: OpenContextTarget;
}) {
  const canOpen = Boolean(target);
  const className = 'flex min-h-11 min-w-0 items-center gap-2 rounded-md border border-neutral-800 bg-neutral-950/50 px-2.5 py-2 text-left';
  const content = (
    <>
      <span className="shrink-0 text-neutral-500">{icon}</span>
      <span className="min-w-0 flex-1">
        <span className="flex min-w-0 items-center gap-1.5">
          {tone && <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${tone.dot}`} aria-hidden="true" />}
          <span className={`truncate text-xs font-semibold ${tone?.text ?? 'text-neutral-100'}`}>{value}</span>
        </span>
        <span className="block truncate font-mono text-ui-sm uppercase text-neutral-500">{label}</span>
        {detail && <span className="mt-0.5 block truncate text-xs text-neutral-500">{detail}</span>}
      </span>
      {canOpen && <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-neutral-600" aria-hidden="true" />}
    </>
  );
  if (!canOpen) return <div className={className}>{content}</div>;
  return (
    <button
      type="button"
      onClick={() => onOpenTarget(target)}
      className={`${className} transition-colors hover:border-neutral-700 hover:bg-neutral-900`}
      title={`Open ${label}`}
    >
      {content}
    </button>
  );
}

function MetricButton({
  metric,
  onOpenTarget,
}: {
  metric: StrategicWorkSessionMetric;
  onOpenTarget: OpenContextTarget;
}) {
  const canOpen = Boolean(metric.target);
  const className = 'min-h-11 min-w-0 rounded-md border border-neutral-800 bg-neutral-950/50 px-2.5 py-2 text-left';
  const content = (
    <>
      <span className={`block truncate text-sm font-semibold ${metricToneClass(metric.tone)}`}>
        {formatStrategicMetricValue(metric)}
      </span>
      <span className="mt-0.5 block truncate text-ui-sm font-semibold uppercase text-neutral-500">
        {metric.label}
        {metric.tone && metric.tone !== 'neutral' ? ` / ${metric.tone}` : ''}
      </span>
    </>
  );
  if (!canOpen) return <div className={className}>{content}</div>;
  return (
    <button
      type="button"
      onClick={() => onOpenTarget(metric.target)}
      className={`${className} transition-colors hover:border-neutral-700 hover:bg-neutral-900`}
      title={`Open ${metric.label}`}
    >
      {content}
    </button>
  );
}

function WorkSessionActionRow({
  action,
  onOpenTarget,
}: {
  action: StrategicWorkSessionAction;
  onOpenTarget: OpenContextTarget;
}) {
  const target = normalizeActionTarget(action);
  const canOpen = Boolean(action.enabled && target);
  return (
    <button
      type="button"
      disabled={!canOpen}
      onClick={() => onOpenTarget(target, action.kind === 'reply' ? 'reply' : 'highlight')}
      className="flex min-h-11 min-w-0 items-center gap-2 rounded-md border border-neutral-800 bg-neutral-950/50 px-2.5 py-2 text-left transition-colors hover:border-neutral-700 hover:bg-neutral-900 disabled:cursor-default disabled:opacity-60"
      title={canOpen ? `Open ${action.label}` : action.label}
    >
      <span className="min-w-0 flex-1">
        <span className="flex min-w-0 items-center gap-1.5">
          <span className="truncate text-xs font-semibold text-neutral-100">{action.label}</span>
          {!action.enabled && (
            <span className="shrink-0 rounded border border-neutral-800 px-1 text-ui-sm font-semibold uppercase text-neutral-500">
              blocked
            </span>
          )}
        </span>
        <span className="mt-0.5 block truncate text-ui-sm font-semibold uppercase text-neutral-500">
          {compactDetail(action.kind, action.source, targetLabel(target)) ?? 'session action'}
        </span>
        {workSessionActionDetail(action) && (
          <span className="mt-0.5 block line-clamp-2 text-xs leading-relaxed text-neutral-500">
            {workSessionActionDetail(action)}
          </span>
        )}
      </span>
      {canOpen && <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-neutral-600" aria-hidden="true" />}
    </button>
  );
}

function AttentionCountPill({ label, value }: { label: string; value: number }) {
  return (
    <span className="inline-flex min-h-8 items-center gap-1.5 rounded-md border border-neutral-800 bg-neutral-950/50 px-2 text-xs text-neutral-500">
      <span className="font-semibold text-neutral-100">{formatCompactNumber(value)}</span>
      <span className="truncate">{label}</span>
    </span>
  );
}

export function StrategicContextWorkspace({
  open,
  state,
  workbenchState,
  savingSession = false,
  savingSessionRefs = false,
  accountSpend = null,
  sessionActive = false,
  sessionStatus = null,
  sessionState = null,
  sessionReason = null,
  sessionBusy = false,
  sessionRunners = null,
  sessionSelectedRunnerId = null,
  operatorDecisionSummary = null,
  projectId,
  onSelectRunner,
  onStartSession,
  onStopSession,
  onClose,
  onOpenTarget,
  onOpenMessageTarget,
  onModeChange,
  onOpenAccounts,
  onOpenSpend,
  onUpdateSessionRefs,
}: StrategicContextWorkspaceProps) {
  useEffect(() => {
    if (!open) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onClose, open]);

  // ── Hooks and memoised derivations (must be before early return for hooks rules) ──

  const contextPackage = state?.context_package ?? null;
  const canvas = contextPackage?.canvas ?? null;
  const itemCount = contextPackage?.item_count ?? 0;
  const tokenCount = contextPackage?.token_count ?? 0;
  const attentionCounts = state?.attention?.counts ?? null;
  const workSession = state?.work_session ?? null;

  const selectedRefs = useMemo(
    () => workbenchState?.selected_refs ?? state?.work_session?.selected_refs ?? state?.workbench?.selected_refs ?? [],
    [workbenchState?.selected_refs, state?.work_session?.selected_refs, state?.workbench?.selected_refs],
  );
  const resources = useMemo(
    () => contextPackage?.resources ?? [],
    [contextPackage?.resources],
  );

  const descriptorByRef = useMemo(() => {
    const descriptors = state?.work_session?.selected_ref_descriptors ?? [];
    return new Map(descriptors.map(descriptor => [contextRefKey(descriptor), descriptor]));
  }, [state?.work_session?.selected_ref_descriptors]);

  const selectedRefDescriptors = useMemo(
    () => selectedRefs.map(ref => descriptorByRef.get(contextRefKey(ref)) ?? fallbackRefDescriptor(ref)),
    [selectedRefs, descriptorByRef],
  );
  const selectedKeys = useMemo(
    () => new Set(selectedRefs.map(contextRefKey)),
    [selectedRefs],
  );
  const nodeLabelById = useMemo(
    () => new Map((canvas?.nodes ?? []).map(node => [node.id, node.label])),
    [canvas?.nodes],
  );
  const contextMapDescriptor = useMemo(
    () => (contextPackage?.descriptors ?? []).find(d => d.kind === 'context_map') ?? null,
    [contextPackage?.descriptors],
  );
  const artifactDescriptors = useMemo(
    () => (contextPackage?.descriptors ?? []).filter(d => d.kind !== 'context_map'),
    [contextPackage?.descriptors],
  );
  // Artifact IDs already shown as descriptors (strip "artifact:" prefix to get UUID).
  // Used below to skip rendering duplicate media in the Evidence section.
  const promotedArtifactIds = useMemo(
    () => new Set(
      artifactDescriptors
        .map(d => (typeof d.id === 'string' && d.id.startsWith('artifact:') ? d.id.slice('artifact:'.length) : null))
        .filter((id): id is string => id !== null),
    ),
    [artifactDescriptors],
  );
  const canvasResources = useMemo(
    () => resources.filter(r => r.kind === 'canvas'),
    [resources],
  );
  const listedResources = useMemo(
    () => resources.filter(r => r.kind !== 'canvas'),
    [resources],
  );
  const syntheticCanvasRef = useMemo(
    () => (canvas ? selectedRefFromCanvas(canvas) : null),
    [canvas],
  );
  const nodes = canvas?.nodes ?? [];
  const edges = canvas?.edges ?? [];
  const canvasResourceRows = useMemo(
    () => (canvasResources.length > 0
      ? canvasResources
      : syntheticCanvasRef
        ? [{
            kind: syntheticCanvasRef.kind,
            id: syntheticCanvasRef.id,
            label: syntheticCanvasRef.label ?? 'Strategic context map',
            summary: canvasSummaryDetail(canvas, canvas?.nodes?.length ?? 0, canvas?.edges?.length ?? 0),
          } satisfies StrategicContextResourceRef]
        : []),
    [canvasResources, syntheticCanvasRef, canvas],
  );
  const drilldowns = useMemo(
    () => [...(contextPackage?.drilldowns ?? []), ...(canvas?.drilldowns ?? [])],
    [contextPackage?.drilldowns, canvas?.drilldowns],
  );
  const packageMeta = useMemo(
    () => [
      itemCount > 0 ? `${formatCompactNumber(itemCount)} items` : null,
      tokenCount > 0 ? `${formatCompactNumber(tokenCount)} tokens` : null,
      contextPackage?.updated_at ? `updated ${contextPackage.updated_at}` : null,
    ].filter(Boolean).join(' / '),
    [itemCount, tokenCount, contextPackage?.updated_at],
  );
  const workSessionActions = useMemo(
    () => (workSession?.actions ?? []).slice(0, 4),
    [workSession?.actions],
  );
  const workSessionMetrics = useMemo(
    () => rankStrategicWorkSessionMetrics(workSession?.curated_metrics ?? [], 6),
    [workSession?.curated_metrics],
  );
  const attentionPills = useMemo(
    () => (attentionCounts
      ? [
          { label: 'needs', value: attentionCounts.needs_operator },
          { label: 'watch', value: attentionCounts.watch },
          { label: 'unread', value: attentionCounts.unread },
          { label: 'questions', value: attentionCounts.questions },
          { label: 'approvals', value: attentionCounts.approvals },
          { label: 'blocked', value: attentionCounts.blocked_tasks },
          { label: 'failed', value: attentionCounts.failed_tasks },
        ].filter(item => item.value > 0).slice(0, 6)
      : []),
    [attentionCounts],
  );

  // Early return when panel is closed
  if (!open) return null;

  // Non-memoised simple derivations (cheap lookups, single access)
  const tone = contextStatusTone(contextPackage?.status);
  const knowledgeEvidence = contextPackage?.evidence?.knowledge?.items ?? [];
  const artifactEvidence = contextPackage?.evidence?.artifacts?.items ?? [];
  const artifactLiveCount: number = (contextPackage?.evidence?.artifacts as { live_count?: number } | undefined)?.live_count ?? 0;
  const sessionMode = normalizeStrategicInteractionMode(workbenchState?.interaction_mode ?? workSession?.mode);
  const localRunner = state?.runtime?.local_runner ?? null;
  const runtimeWork = state?.runtime?.work ?? null;
  const localRunnerStatus = localRunner ? localRunnerTone(localRunner) : null;
  const runtimeWorkStatus = runtimeWork ? runtimeWorkTone(runtimeWork) : null;
  const workSessionEvents = workSession?.events?.items ?? [];
  const showDescriptorOnlyContextMap = Boolean(contextMapDescriptor && !canvas);
  const strategicMemoryNotes = state?.strategic_memory_notes ?? [];

  const handleOpenTarget = (
    target?: StrategicContextTarget | null,
    messageAction?: StrategicMessageTarget['action'],
  ): void => {
    if (!target) return;
    const messageTarget = messageTargetFromContextTarget(target, messageAction);
    if (messageTarget && onOpenMessageTarget) {
      onClose();
      onOpenMessageTarget(messageTarget);
      return;
    }
    onClose();
    onOpenTarget(target);
  };
  const keepRef = (ref: StrategicSelectedRef): void => {
    onUpdateSessionRefs?.(refs => upsertStrategicSelectedRef(refs, ref));
  };
  const removeRef = (ref: Pick<StrategicSelectedRef, 'kind' | 'id'>): void => {
    onUpdateSessionRefs?.(refs => removeStrategicSelectedRef(refs, ref));
  };
  const renderKeepableRow = ({
    key,
    label,
    kind,
    detail,
    target,
    ref,
    badge,
  }: {
    key: string;
    label: string;
    kind: string;
    detail?: string | null;
    target?: StrategicContextTarget | null;
    ref: StrategicSelectedRef;
    badge?: ReactNode;
  }) => {
    const selected = selectedKeys.has(contextRefKey(ref));
    return (
      <ContextRow
        key={key}
        label={label}
        kind={kind}
        detail={detail}
        target={target}
        selected={selected}
        saving={savingSessionRefs}
        badge={badge}
        onOpenTarget={handleOpenTarget}
        onKeep={onUpdateSessionRefs && !selected ? () => keepRef(ref) : undefined}
        onRemove={onUpdateSessionRefs && selected ? () => removeRef(ref) : undefined}
      />
    );
  };

  return (
    <div className="fixed inset-0 z-[65] flex justify-end" role="dialog" aria-modal="true" aria-label="Strategic context">
      <button
        type="button"
        className="absolute inset-0 cursor-default bg-black/50"
        aria-label="Close Strategic context"
        onClick={onClose}
      />
      <div className="relative flex h-full w-full max-w-full flex-col overflow-hidden border-l border-workbench-border bg-workbench-base shadow-2xl md:max-w-3xl">
        <header className="flex min-w-0 shrink-0 items-center justify-between gap-3 border-b border-workbench-border bg-workbench-shell px-3 py-2.5 md:px-4">
          <div className="min-w-0">
            <div className="flex min-w-0 items-center gap-2">
              <Network className="h-4 w-4 shrink-0 text-neutral-500" aria-hidden="true" />
              <h2 className="truncate text-lg font-semibold text-neutral-100">Strategic context</h2>
              <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${tone.dot}`} aria-hidden="true" />
              <span className={`shrink-0 text-ui-sm font-semibold uppercase ${tone.text}`}>{tone.label}</span>
            </div>
            {contextPackage && (
              <p className="mt-0.5 truncate text-xs text-neutral-500">
                {contextPackage.title}
                {packageMeta ? ` / ${packageMeta}` : ''}
              </p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded-md text-neutral-500 transition-colors hover:bg-neutral-900 hover:text-neutral-100"
            title="Close Strategic context"
            aria-label="Close Strategic context"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </header>

        <div className="min-h-0 flex-1 overflow-auto p-3 custom-scrollbar md:p-4">
          <div className="grid min-w-0 max-w-full grid-cols-1 gap-3">
            <Section
              title="Session"
              meta={sessionModeLabel(sessionMode)}
              testId="strategic-context-workspace-session"
            >
              <div className="space-y-3">
                <StrategicSessionControl
                  mode={sessionMode}
                  saving={savingSession}
                  accountSpend={accountSpend}
                  sessionActive={sessionActive}
                  sessionStatus={sessionStatus}
                  sessionState={sessionState}
                  sessionReason={sessionReason}
                  sessionBusy={sessionBusy}
                  sessionRunners={sessionRunners}
                  sessionSelectedRunnerId={sessionSelectedRunnerId}
                  operatorDecisionSummary={operatorDecisionSummary}
                  onSelectRunner={onSelectRunner}
                  onStartSession={onStartSession}
                  onStopSession={onStopSession}
                  onModeChange={onModeChange}
                  onOpenAccounts={onOpenAccounts ? () => {
                    onClose();
                    onOpenAccounts();
                  } : undefined}
                  onOpenSpend={onOpenSpend ? () => {
                    onClose();
                    onOpenSpend();
                  } : undefined}
                  className="items-stretch sm:items-stretch sm:justify-start"
                />

                <div className="grid gap-2 md:grid-cols-2">
                  {localRunner && localRunnerStatus && (
                    <SessionSummaryButton
                      icon={<Server className="h-4 w-4" aria-hidden="true" />}
                      label="Runner"
                      value={localRunnerStatus.label}
                      detail={localRunnerDetail(localRunner)}
                      tone={localRunnerStatus}
                      target={{ surface: 'runtime', section: 'devices' }}
                      onOpenTarget={handleOpenTarget}
                    />
                  )}
                  {runtimeWork && runtimeWorkStatus && (
                    <SessionSummaryButton
                      icon={<Cpu className="h-4 w-4" aria-hidden="true" />}
                      label="Compute"
                      value={runtimeWorkStatus.label}
                      detail={runtimeWorkDetail(runtimeWork)}
                      tone={runtimeWorkStatus}
                      target={{ surface: 'runtime', section: 'compute' }}
                      onOpenTarget={handleOpenTarget}
                    />
                  )}
                  {state?.attention && (
                    <SessionSummaryButton
                      icon={<CircleAlert className="h-4 w-4" aria-hidden="true" />}
                      label="Attention"
                      value={state.attention.level.replace(/_/g, ' ')}
                      detail={state.attention.summary}
                      tone={{
                        dot: state.attention.level === 'needs_operator' ? 'bg-amber-300' : 'bg-neutral-500',
                        text: state.attention.level === 'needs_operator' ? 'text-amber-300' : 'text-neutral-300',
                      }}
                      target={{ surface: 'messages' }}
                      onOpenTarget={handleOpenTarget}
                    />
                  )}
                  <SessionSummaryButton
                    icon={<Activity className="h-4 w-4" aria-hidden="true" />}
                    label="Work"
                    value={`${formatCompactNumber(state?.tasks?.counts.running ?? 0)} running`}
                    detail={`${formatCompactNumber(state?.tasks?.counts.queued ?? 0)} queued / ${formatCompactNumber(state?.tasks?.counts.blocked ?? 0)} blocked`}
                    target={{ surface: 'pipeline', section: 'flow' }}
                    onOpenTarget={handleOpenTarget}
                  />
                </div>

                {attentionPills.length > 0 && (
                  <div className="flex min-w-0 flex-wrap gap-1.5" aria-label="Attention counts">
                    {attentionPills.map(item => (
                      <AttentionCountPill key={item.label} label={item.label} value={item.value} />
                    ))}
                  </div>
                )}

                {workSessionMetrics.length > 0 && (
                  <div className="grid gap-2 sm:grid-cols-3" data-testid="strategic-session-metrics">
                    {workSessionMetrics.map(metric => (
                      <MetricButton key={metric.id} metric={metric} onOpenTarget={handleOpenTarget} />
                    ))}
                  </div>
                )}

                {workSessionActions.length > 0 && (
                  <div className="grid gap-2 lg:grid-cols-2" data-testid="strategic-session-actions">
                    {workSessionActions.map(action => (
                      <WorkSessionActionRow key={action.id} action={action} onOpenTarget={handleOpenTarget} />
                    ))}
                  </div>
                )}
              </div>
            </Section>

            {state?.model_integrity && state.model_integrity.counts.total > 0 && (
              <Section
                title="Model integrity"
                meta={`${state.model_integrity.counts.total} conflict${state.model_integrity.counts.total !== 1 ? 's' : ''}`}
                testId="strategic-context-workspace-integrity"
              >
                <div className={CONTEXT_LIST_CLASS}>
                  {state.model_integrity.orphaned_gaps.map(gap => (
                    <div key={`orphan:${gap.id}`} className="border-t border-neutral-800/70 py-2 first:border-t-0">
                      <span className="block truncate text-sm font-semibold text-neutral-100">{gap.name || gap.id}</span>
                      <span className="mt-0.5 block text-ui-sm font-semibold uppercase text-amber-400">orphaned gap</span>
                      <span className="mt-0.5 block truncate text-xs text-neutral-500">
                        {gap.status === 'blocked' ? 'blocked gap' : 'gap'} {gap.id.slice(0, 8)} has no active direction
                      </span>
                      {gap.recovery_hint && (
                        <span className="mt-0.5 block text-xs text-emerald-400">→ {gap.recovery_hint}</span>
                      )}
                    </div>
                  ))}
                  {state.model_integrity.ungrounded_directions.map(dir => (
                    <div key={`ungrounded:${dir.id}`} className="border-t border-neutral-800/70 py-2 first:border-t-0">
                      <span className="block truncate text-sm font-semibold text-neutral-100">{dir.text || dir.id}</span>
                      <span className="mt-0.5 block text-ui-sm font-semibold uppercase text-amber-400">ungrounded direction</span>
                      <span className="mt-0.5 block truncate text-xs text-neutral-500">
                        {dir.reason === 'no_gap_id' ? 'no gap linked' : `gap ${dir.gap_id} is not active`}
                      </span>
                      {dir.recovery_hint && (
                        <span className="mt-0.5 block text-xs text-emerald-400">→ {dir.recovery_hint}</span>
                      )}
                    </div>
                  ))}
                  {state.model_integrity.stale_active_moves.map(item => (
                    <div key={`stale:${item.gap_id}`} className="border-t border-neutral-800/70 py-2 first:border-t-0">
                      <span className="block truncate text-sm font-semibold text-neutral-100">{item.gap_name || item.gap_id}</span>
                      <span className="mt-0.5 block text-ui-sm font-semibold uppercase text-amber-400">stale active_moves</span>
                      <span className="mt-0.5 block truncate text-xs text-neutral-500">
                        {`${item.stale_direction_ids.length} stale direction${item.stale_direction_ids.length !== 1 ? 's' : ''} in active_moves`}
                      </span>
                      {item.recovery_hint && (
                        <span className="mt-0.5 block text-xs text-emerald-400">→ {item.recovery_hint}</span>
                      )}
                    </div>
                  ))}
                  {(state.model_integrity.criterion_link_conflicts ?? []).map(link => (
                    <div key={`crit:${link.direction_id}:${link.criterion_key}`} className="border-t border-neutral-800/70 py-2 first:border-t-0">
                      <span className="block truncate text-sm font-semibold text-neutral-100">{link.direction_text || link.direction_id}</span>
                      <span className="mt-0.5 block text-ui-sm font-semibold uppercase text-amber-400">
                        {link.classification === 'inactive' ? 'inactive criterion link' : 'dangling criterion link'}
                      </span>
                      <span className="mt-0.5 block truncate text-xs text-neutral-500">
                        criterion {link.criterion_key} {link.classification === 'inactive' ? 'is retracted or superseded' : 'is not in the open version'}
                      </span>
                      {link.recovery_hint && (
                        <span className="mt-0.5 block text-xs text-emerald-400">→ {link.recovery_hint}</span>
                      )}
                    </div>
                  ))}
                </div>
              </Section>
            )}

            {workSessionEvents.length > 0 && (
              <Section
                title="Recent flow"
                meta={workSession?.events?.has_more ? `${workSessionEvents.length}+` : `${workSessionEvents.length}`}
                testId="strategic-context-workspace-events"
              >
                <StrategicSessionEvents
                  events={workSessionEvents}
                  limit={10}
                  density="comfortable"
                  onOpenTarget={handleOpenTarget}
                />
              </Section>
            )}

            {strategicMemoryNotes.length > 0 && (
              <Section
                title="Memory"
                meta={`${strategicMemoryNotes.length}`}
                testId="strategic-context-workspace-memory"
              >
                <div className={CONTEXT_LIST_CLASS}>
                  {strategicMemoryNotes.map((item, idx) => (
                    <div key={idx} className="border-t border-neutral-800/70 py-2.5 first:border-t-0">
                      <span className="block whitespace-pre-wrap text-sm text-neutral-100">{item.note}</span>
                      <span className="mt-0.5 block text-ui-sm text-neutral-500">{item.set_at}</span>
                    </div>
                  ))}
                </div>
              </Section>
            )}

            <Section
              title="Kept context"
              meta={`${selectedRefDescriptors.length}/${MAX_STRATEGIC_SELECTED_REFS}`}
              testId="strategic-context-workspace-kept"
            >
              {selectedRefDescriptors.length > 0 ? (
                <div className={CONTEXT_LIST_CLASS}>
                  {selectedRefDescriptors.map(ref => (
                    <ContextRow
                      key={contextRefKey(ref)}
                      label={ref.label}
                      kind={ref.kind}
                      detail={ref.summary ?? null}
                      target={ref.target}
                      selected
                      saving={savingSessionRefs}
                      badge={<StatusBadge status={ref.status} />}
                      onOpenTarget={handleOpenTarget}
                      onRemove={onUpdateSessionRefs ? () => removeRef(ref) : undefined}
                    />
                  ))}
                </div>
              ) : (
                <EmptyState>No context is pinned for this Strategic session.</EmptyState>
              )}
            </Section>

            {drilldowns.length > 0 && (
              <Section title="Open" meta={`${drilldowns.length}`} testId="strategic-context-workspace-drilldowns">
                <div className="grid gap-2 sm:grid-cols-2">
                  {drilldowns.map(drilldown => (
                    <button
                      key={`${drilldown.id}:${targetLabel(drilldown.target) ?? 'target'}`}
                      type="button"
                      onClick={() => handleOpenTarget(drilldown.target)}
                      className="flex min-h-11 min-w-0 items-center justify-between gap-2 rounded-md border border-neutral-800 bg-neutral-950/50 px-3 py-2 text-left transition-colors hover:border-neutral-700 hover:bg-neutral-900"
                    >
                      <span className="min-w-0">
                        <span className="block truncate text-sm font-semibold text-neutral-100">{drilldown.label}</span>
                        <span className="mt-0.5 block truncate text-ui-sm font-semibold uppercase text-neutral-500">
                          {targetLabel(drilldown.target)}
                        </span>
                      </span>
                      <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-neutral-600" aria-hidden="true" />
                    </button>
                  ))}
                </div>
              </Section>
            )}

            {contextPackage && (artifactDescriptors.length > 0 || showDescriptorOnlyContextMap) && (
              <Section
                title={artifactDescriptors.length > 0 ? 'Artifacts' : 'Context map'}
                meta={`${formatCompactNumber(artifactDescriptors.length + (showDescriptorOnlyContextMap ? 1 : 0))}`}
                testId="strategic-context-workspace-artifacts"
              >
                <StrategicContextArtifactSlot
                  contextPackage={contextPackage}
                  onOpenTarget={handleOpenTarget}
                  showHeader={false}
                  showMapDescriptor={showDescriptorOnlyContextMap}
                  projectId={projectId}
                />
              </Section>
            )}

            <Section
              title={canvas?.title ?? 'Context map'}
              meta={`${formatCompactNumber(canvas?.node_count ?? nodes.length)} nodes / ${formatCompactNumber(canvas?.edge_count ?? edges.length)} links`}
              testId="strategic-context-workspace-map"
            >
              <StrategicContextMap canvas={canvas} onOpenTarget={handleOpenTarget} />
              {canvasResourceRows.length > 0 && (
                <div className={`mt-3 ${CONTEXT_LIST_CLASS}`} data-testid="strategic-context-workspace-map-resource">
                  {canvasResourceRows.map(resource => renderKeepableRow({
                    key: `${resource.kind}:${resource.id}`,
                    label: resource.label,
                    kind: resource.kind,
                    detail: resource.summary ?? canvasSummaryDetail(canvas, nodes.length, edges.length),
                    target: resource.target,
                    ref: selectedRefFromResource(resource),
                  }))}
                </div>
              )}
              {nodes.length > 0 && (
                <div className={`mt-3 ${CONTEXT_LIST_CLASS}`}>
                  {nodes.map(node => renderKeepableRow({
                    key: node.id,
                    label: node.label,
                    kind: node.kind,
                    detail: node.summary ?? null,
                    target: node.target,
                    ref: selectedRefFromNode(node),
                    badge: (
                      <>
                        {node.count != null && (
                          <span className="shrink-0 rounded border border-neutral-800 px-1 text-ui-sm font-semibold text-neutral-500">
                            {formatCompactNumber(node.count)}
                          </span>
                        )}
                        <StatusBadge status={node.status} />
                      </>
                    ),
                  }))}
                </div>
              )}
              {edges.length > 0 && (
                <div className={`mt-3 ${CONTEXT_LIST_CLASS}`} data-testid="strategic-context-workspace-edges">
                  {edges.map((edge, index) => (
                    <EdgeRow
                      key={`${edge.source}:${edge.target}:${edge.label ?? 'link'}:${index}`}
                      source={nodeLabelById.get(edge.source) ?? edge.source}
                      target={nodeLabelById.get(edge.target) ?? edge.target}
                      label={edge.label}
                    />
                  ))}
                </div>
              )}
            </Section>

            <Section title="Resources" meta={`${listedResources.length}`} testId="strategic-context-workspace-resources">
              {listedResources.length > 0 ? (
                <div className={CONTEXT_LIST_CLASS}>
                  {listedResources.map(resource => renderKeepableRow({
                    key: `${resource.kind}:${resource.id}`,
                    label: resource.label,
                    kind: resource.kind,
                    detail: resource.summary,
                    target: resource.target,
                    ref: selectedRefFromResource(resource),
                  }))}
                </div>
              ) : (
                <EmptyState>
                  {canvasResourceRows.length > 0
                    ? 'No additional resources are attached beyond the context map.'
                    : 'No resources are attached to this package.'}
                </EmptyState>
              )}
            </Section>

            <Section
              title="Evidence"
              meta={[
                `${knowledgeEvidence.length + artifactEvidence.length}`,
                artifactLiveCount > 0 ? `${artifactLiveCount} live` : null,
              ].filter(Boolean).join(' · ')}
              testId="strategic-context-workspace-evidence"
            >
              {knowledgeEvidence.length === 0 && artifactEvidence.length === 0 ? (
                <EmptyState>No evidence is attached to this package.</EmptyState>
              ) : (
                <div className="grid gap-3 lg:grid-cols-2">
                  {knowledgeEvidence.length > 0 && (
                    <div className={CONTEXT_LIST_CLASS}>
                      {knowledgeEvidence.map(item => {
                        const ref = selectedRefFromKnowledgeEvidence(item);
                        return renderKeepableRow({
                          key: `knowledge:${item.id}`,
                          label: ref.label ?? 'Knowledge document',
                          kind: item.kind,
                          detail: item.summary ?? item.source_kind ?? null,
                          target: item.target,
                          ref,
                        });
                      })}
                    </div>
                  )}
                  {artifactEvidence.length > 0 && (
                    <div className={CONTEXT_LIST_CLASS}>
                      {artifactEvidence.map(item => {
                        const ref = selectedRefFromArtifactEvidence(item);
                        const label = ref.label ?? 'Artifact';
                        const isPromoted = promotedArtifactIds.has(String(item.id));
                        return (
                          <div key={`artifact:${item.id}`} className="min-w-0">
                            {renderKeepableRow({
                              key: `artifact-row:${item.id}`,
                              label,
                              kind: item.artifact_type ?? item.kind,
                              detail: item.task_title ?? item.task_status ?? null,
                              target: item.target,
                              ref,
                            })}
                            {!isPromoted && (
                              <ArtifactViewablePreviews
                                previews={item.viewable_previews}
                                projectId={projectId}
                                label={label}
                              />
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              )}
            </Section>

            {!contextPackage && (
              <Section title="Context package" testId="strategic-context-workspace-empty">
                <EmptyState>No Strategic context package is loaded for this project.</EmptyState>
              </Section>
            )}

            {contextPackage?.sources && contextPackage.sources.length > 0 && (
              <div className="flex min-w-0 flex-wrap gap-1.5 text-ui-sm text-neutral-500" data-testid="strategic-context-workspace-sources">
                <GitBranch className="h-3.5 w-3.5 text-neutral-600" aria-hidden="true" />
                {contextPackage.sources.map(source => (
                  <span key={source.kind} className="rounded border border-neutral-800 bg-neutral-950/60 px-1.5 py-0.5">
                    {source.kind.replace(/_/g, ' ')} {formatCompactNumber(source.count)}
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
