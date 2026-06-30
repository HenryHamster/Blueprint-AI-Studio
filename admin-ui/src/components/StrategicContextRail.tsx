import type { ReactNode } from 'react';
import { Activity, ArrowUpRight, CircleAlert, Cpu, FileText, GitBranch, Network, Plus, Radio, X } from 'lucide-react';
import type { StrategicCycleCompletion, StrategicCycleProgress } from '../hooks/useLogViewer';
import type {
  StrategicContextCanvasNode,
  StrategicContextPackageDescriptor,
  StrategicContextPackageDrilldown,
  StrategicContextResourceRef,
  StrategicLocalRunnerState,
  StrategicMessageTarget,
  StrategicStateTaskFocus,
  StrategicStateTaskFocusBucket,
  StrategicStateTaskFocusItem,
  StrategicRuntimeWorkState,
  StrategicStateAttentionItem,
  StrategicStateResponse,
  StrategicWorkbenchState,
  StrategicWorkSessionRefDescriptor,
} from '../strategicStateApi';
import {
  MAX_STRATEGIC_SELECTED_REFS,
  removeStrategicSelectedRef,
  upsertStrategicSelectedRef,
  type StrategicSelectedRef,
} from '../utils/strategicWorkbenchSession';
import { WorkbenchPanel } from './WorkbenchLayout';

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
  onOpenTask?: (taskId: string) => void;
  onOpenMessages?: (target?: StrategicMessageTarget) => void;
  onOpenKnowledge?: (documentId?: string | null, directionId?: string | null) => void;
  workbenchState?: StrategicWorkbenchState | null;
  savingSessionRefs?: boolean;
  onUpdateSessionRefs?: (
    updateRefs: (refs: StrategicWorkbenchState['selected_refs']) => StrategicWorkbenchState['selected_refs']
  ) => void;
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
  if ((work.held_approvals?.count ?? 0) > 0 || (work.parked_wakes?.counts.problem ?? 0) > 0) {
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
    (work.compute_jobs?.counts.active ?? 0) > 0
      ? pluralize(work.compute_jobs.counts.active, 'compute job')
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
    (work.compute_jobs?.counts.failed_recent ?? 0) > 0 ||
    (work.compute_jobs?.counts.timed_out_recent ?? 0) > 0 ||
    (work.held_approvals?.count ?? 0) > 0 ||
    (work.parked_wakes?.counts.active ?? 0) > 0 ||
    (work.parked_wakes?.counts.problem ?? 0) > 0 ||
    (work.compute_jobs?.counts.terminal_recent ?? 0) > 0
  );
}

function RailButton({
  children,
  onClick,
}: {
  children: ReactNode;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
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

function selectedRefFromResource(resource: StrategicContextResourceRef): StrategicSelectedRef {
  return {
    kind: resource.kind,
    id: resource.id,
    label: resource.label,
  };
}

function selectedRefFromNode(node: StrategicContextCanvasNode): StrategicSelectedRef {
  return {
    kind: node.kind,
    id: node.id,
    label: node.label,
  };
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

function SessionRefAction({
  selected,
  disabled,
  label,
  onUse,
  onRemove,
}: {
  selected: boolean;
  disabled?: boolean;
  label: string;
  onUse?: () => void;
  onRemove?: () => void;
}) {
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
      onClick={onUse}
      className="inline-flex min-h-11 shrink-0 items-center gap-1 rounded-md border border-neutral-800 px-2 text-ui-sm font-semibold text-neutral-400 transition-colors hover:border-neutral-700 hover:bg-neutral-900 hover:text-neutral-100 disabled:opacity-40"
      title={`Keep ${label} in session context`}
      aria-label={`Keep ${label} in session context`}
    >
      <Plus className="h-3.5 w-3.5" aria-hidden="true" />
      <span>Keep</span>
    </button>
  );
}

function ContextResourceButton({
  resource,
  onOpen,
  selected,
  onUse,
  onRemove,
  disabled,
}: {
  resource: StrategicContextResourceRef;
  onOpen?: (resource: StrategicContextResourceRef) => void;
  selected?: boolean;
  onUse?: () => void;
  onRemove?: () => void;
  disabled?: boolean;
}) {
  const openDisabled = !onOpen;
  const detail = resource.kind === 'canvas' ? null : resource.summary;
  return (
    <div className="flex min-w-0 items-center gap-2 rounded-md border border-neutral-800 bg-neutral-950/60 px-2.5 py-2">
      <button
        type="button"
        disabled={openDisabled}
        onClick={() => onOpen?.(resource)}
        className="min-h-11 min-w-0 flex-1 text-left disabled:cursor-default"
      >
        <span className="min-w-0">
          <span className="block truncate text-xs font-semibold text-neutral-200">
            {resource.label}
          </span>
          <span className="mt-0.5 block truncate text-ui-sm font-medium uppercase text-neutral-500">
            {resourceKindLabel(resource.kind)}
            {detail ? ` / ${detail}` : ''}
          </span>
        </span>
      </button>
      {!openDisabled && <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-neutral-600" aria-hidden="true" />}
      {(onUse || onRemove) && (
        <SessionRefAction
          selected={Boolean(selected)}
          disabled={disabled}
          label={resource.label}
          onUse={onUse}
          onRemove={onRemove}
        />
      )}
    </div>
  );
}

function ContextMapNodeRow({
  node,
  selected,
  disabled,
  onOpenTarget,
  onUse,
  onRemove,
}: {
  node: StrategicContextCanvasNode;
  selected?: boolean;
  disabled?: boolean;
  onOpenTarget: (target?: StrategicContextPackageDrilldown['target'] | null) => void;
  onUse?: () => void;
  onRemove?: () => void;
}) {
  const canOpen = Boolean(node.target);
  return (
    <div className="flex min-w-0 items-center gap-2 border-t border-neutral-800/70 py-1.5 first:border-t-0">
      <button
        type="button"
        disabled={!canOpen}
        onClick={() => onOpenTarget(node.target)}
        className="min-h-11 min-w-0 flex-1 text-left disabled:cursor-default"
        title={canOpen ? `Open ${node.label}` : node.label}
      >
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate text-xs font-semibold text-neutral-200">{node.label}</span>
          {node.count != null && (
            <span className="shrink-0 rounded border border-neutral-800 px-1 text-ui-sm font-semibold text-neutral-500">
              {formatCompactNumber(node.count)}
            </span>
          )}
        </span>
        <span className="mt-0.5 block truncate text-ui-sm font-medium uppercase text-neutral-500">
          {resourceKindLabel(node.kind)}
          {node.status ? ` / ${node.status}` : ''}
        </span>
        {node.summary && (
          <span className="mt-0.5 block truncate text-xs text-neutral-600">{node.summary}</span>
        )}
      </button>
      {(onUse || onRemove) && (
        <SessionRefAction
          selected={Boolean(selected)}
          disabled={disabled}
          label={node.label}
          onUse={onUse}
          onRemove={onRemove}
        />
      )}
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

function taskFocusBucket(focus: StrategicStateTaskFocus | null | undefined, key: TaskFocusBucketKey): StrategicStateTaskFocusBucket | null {
  const bucket = focus?.buckets?.[key];
  if (!bucket || bucket.total <= 0) return null;
  return bucket;
}

function taskFocusDetail(item: StrategicStateTaskFocusItem): string {
  const reasons = item.reasons ?? [];
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
  const content = (
    <>
      <div className="flex min-w-0 items-center justify-between gap-2">
        <span className="truncate text-xs font-semibold text-neutral-200">{item.title || item.id}</span>
        <span className="shrink-0 font-mono text-ui-sm text-neutral-600">{shortId(item.id)}</span>
      </div>
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
  onOpenPipeline,
}: {
  focus?: StrategicStateTaskFocus | null;
  onOpenTask?: (taskId: string) => void;
  onOpenPipeline: () => void;
}) {
  const renderedIds = new Set<string>();
  const liveSections = TASK_FOCUS_SECTIONS
    .filter(section => section.key !== 'recent_completed')
    .map(section => {
      const bucket = taskFocusBucket(focus, section.key);
      if (!bucket) return null;
      const items = (bucket.items ?? []).filter(item => !renderedIds.has(item.id)).slice(0, section.limit);
      items.forEach(item => renderedIds.add(item.id));
      return { section, bucket, items };
    })
    .filter((entry): entry is { section: (typeof TASK_FOCUS_SECTIONS)[number]; bucket: StrategicStateTaskFocusBucket; items: StrategicStateTaskFocusItem[] } => Boolean(entry && entry.items.length > 0));
  const completedBucket = taskFocusBucket(focus, 'recent_completed');
  const completedItems = completedBucket
    ? (completedBucket.items ?? []).filter(item => !renderedIds.has(item.id))
    : [];
  const sections = liveSections.length > 0
    ? liveSections
    : completedBucket && completedItems.length > 0
      ? [{ section: TASK_FOCUS_SECTIONS[TASK_FOCUS_SECTIONS.length - 1], bucket: completedBucket, items: completedItems }]
      : [];
  const openTotal = liveSections.reduce((sum, entry) => sum + entry.items.length, 0);

  if (sections.length === 0) return null;

  return (
    <WorkbenchPanel title="Task focus" meta={openTotal > 0 ? `${formatCompactNumber(openTotal)} shown` : 'recent'}>
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
        <div className="pt-1">
          <RailButton onClick={onOpenPipeline}>Pipeline</RailButton>
        </div>
      </div>
    </WorkbenchPanel>
  );
}

function ContextPackageBlock({
  contextPackage,
  sessionRefs,
  selectedRefs,
  savingSessionRefs,
  onOpenResource,
  onOpenTarget,
  onUseRef,
  onRemoveRef,
}: {
  contextPackage?: StrategicContextPackageDescriptor | null;
  sessionRefs: StrategicWorkSessionRefDescriptor[];
  selectedRefs: StrategicSelectedRef[];
  savingSessionRefs?: boolean;
  onOpenResource?: (resource: StrategicContextResourceRef) => void;
  onOpenTarget: (target?: StrategicContextPackageDrilldown['target'] | null) => void;
  onUseRef?: (ref: StrategicSelectedRef) => void;
  onRemoveRef?: (ref: Pick<StrategicSelectedRef, 'kind' | 'id'>) => void;
}) {
  if (!contextPackage) return null;
  const tone = contextPackageTone(contextPackage.status);
  const sources = (contextPackage.sources ?? []).slice(0, 5);
  const itemCount = Number(contextPackage.item_count ?? 0);
  const resources = (contextPackage.resources ?? []).slice(0, 4);
  const mapNodes = (contextPackage.canvas?.nodes ?? [])
    .filter(node => node.kind !== 'project')
    .slice(0, 5);
  const nodeCount = contextPackage.canvas?.node_count ?? null;
  const edgeCount = contextPackage.canvas?.edge_count ?? null;
  const selectedKeys = new Set(selectedRefs.map(sessionRefKey));

  return (
    <div className="border-b border-neutral-800 pb-3">
      <div className="flex min-w-0 gap-2">
        <Network className="mt-0.5 h-4 w-4 shrink-0 text-neutral-500" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">
            <span className="truncate text-xs font-semibold text-neutral-100">{contextPackage.title}</span>
            <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${tone.dot}`} aria-hidden="true" />
            <span className={`shrink-0 text-ui-sm font-semibold uppercase ${tone.text}`}>
              {tone.label}
            </span>
          </div>
          <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-neutral-500">
            {itemCount > 0 && <span>{formatCompactNumber(itemCount)} items</span>}
            {nodeCount != null && (
              <span>
                {formatCompactNumber(nodeCount)} nodes
                {edgeCount != null ? ` / ${formatCompactNumber(edgeCount)} links` : ''}
              </span>
            )}
          </div>
        </div>
      </div>
      {sources.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {sources.map(source => (
            <span
              key={source.kind}
              className="rounded border border-neutral-800 bg-neutral-950/70 px-1.5 py-0.5 text-ui-sm font-medium text-neutral-500"
            >
              {source.kind.replace(/_/g, ' ')} {formatCompactNumber(source.count)}
            </span>
          ))}
        </div>
      )}
      <SessionContextList
        refs={sessionRefs}
        onOpenTarget={onOpenTarget}
        onRemove={onRemoveRef}
        disabled={savingSessionRefs}
      />
      {mapNodes.length > 0 && (
        <div className="mt-2" data-testid="strategic-context-map">
          <div className="mb-1 text-ui-sm font-semibold uppercase text-neutral-500">Context map</div>
          <div className="rounded-md border border-neutral-800 bg-neutral-950/40 px-2.5">
            {mapNodes.map(node => {
              const ref = selectedRefFromNode(node);
              const key = sessionRefKey(ref);
              return (
                <ContextMapNodeRow
                  key={node.id}
                  node={node}
                  selected={selectedKeys.has(key)}
                  disabled={savingSessionRefs}
                  onOpenTarget={onOpenTarget}
                  onUse={onUseRef ? () => onUseRef(ref) : undefined}
                  onRemove={onRemoveRef ? () => onRemoveRef(ref) : undefined}
                />
              );
            })}
          </div>
        </div>
      )}
      {resources.length > 0 && (
        <div className="mt-2 space-y-1.5" data-testid="strategic-context-resources">
          {resources.map(resource => (
            (() => {
              const ref = selectedRefFromResource(resource);
              const key = sessionRefKey(ref);
              return (
                <ContextResourceButton
                  key={`${resource.kind}:${resource.id}`}
                  resource={resource}
                  selected={selectedKeys.has(key)}
                  disabled={savingSessionRefs}
                  onOpen={onOpenResource}
                  onUse={onUseRef ? () => onUseRef(ref) : undefined}
                  onRemove={onRemoveRef ? () => onRemoveRef(ref) : undefined}
                />
              );
            })()
          ))}
        </div>
      )}
    </div>
  );
}

export function StrategicContextRail({
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
  onOpenTask,
  onOpenMessages,
  onOpenKnowledge,
  workbenchState,
  savingSessionRefs = false,
  onUpdateSessionRefs,
}: StrategicContextRailProps) {
  const activeCycle = state?.strategic.active_cycle ?? null;
  const localRunner = state?.runtime?.local_runner ?? null;
  const runtimeWork = state?.runtime?.work ?? null;
  const pendingSteer = state?.strategic.refresh.pending ?? null;
  const latestCycle = state?.strategic.latest_cycle ?? null;
  const taskCounts = state?.tasks.counts;
  const attention = state?.attention ?? null;
  const attentionItems = attention?.items.slice(0, 2) ?? [];
  const attentionNeeds = attention?.counts.needs_operator ?? 0;
  const attentionWatch = attention?.counts.watch ?? 0;
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
  const selectedRefs = workbenchState?.selected_refs ?? state?.work_session?.selected_refs ?? state?.workbench?.selected_refs ?? [];
  const workSessionDescriptors = state?.work_session?.selected_ref_descriptors ?? [];
  const descriptorByRef = new Map(workSessionDescriptors.map(descriptor => [sessionRefKey(descriptor), descriptor]));
  const selectedRefDescriptors = selectedRefs.map(ref => descriptorByRef.get(sessionRefKey(ref)) ?? fallbackRefDescriptor(ref));

  function attentionItemAction(item: StrategicStateAttentionItem): (() => void) | undefined {
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

  function openContextTarget(target?: StrategicContextPackageDrilldown['target'] | null): void {
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
      if (onOpenMessages) onOpenMessages();
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

  function openContextResource(resource: StrategicContextResourceRef): void {
    openContextTarget(resource.target);
  }

  function useSessionRef(ref: StrategicSelectedRef): void {
    if (!onUpdateSessionRefs) return;
    onUpdateSessionRefs(refs => upsertStrategicSelectedRef(refs, ref));
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
        {(pendingSteer?.operator_count || latestCycle) && (
          <div className="mt-3 border-t border-neutral-800 pt-2">
            {pendingSteer?.operator_count ? (
              <StatRow label="queued steers" value={pendingSteer.operator_count} />
            ) : null}
            {latestCycle ? (
              <StatRow
                label="last cycle"
                value={`${latestCycle.directions_created} opened / ${latestCycle.tasks_spawned} tasks`}
              />
            ) : null}
          </div>
        )}
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
                {firstRuntimeJob?.task_title && (
                  <p className="mt-1 truncate text-ui-sm text-neutral-600">
                    {firstRuntimeJob.task_title}
                  </p>
                )}
              </div>
              <RailButton onClick={onOpenRuntimeCompute ?? onOpenRuntimeHealth}>Compute</RailButton>
            </div>
          </div>
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
            {attentionItems.length > 0 && (
              <div className="mt-2 space-y-1.5">
                {attentionItems.map(item => (
                  <AttentionItem key={item.id} item={item} onClick={attentionItemAction(item)} />
                ))}
              </div>
            )}
          </div>
        )}
        <ShaFreshnessBlock shaFreshness={shaFreshness} />
      </WorkbenchPanel>

      <TaskFocusBlock
        focus={state?.tasks.focus ?? null}
        onOpenTask={onOpenTask}
        onOpenPipeline={onOpenPipeline}
      />

      <WorkbenchPanel title="Problem" meta={state?.problem_model.revision ? `r${state.problem_model.revision}` : undefined}>
        <div className="flex min-w-0 gap-2">
          <Radio className="mt-0.5 h-4 w-4 shrink-0 text-neutral-500" aria-hidden="true" />
          <p className="line-clamp-3 text-xs leading-relaxed text-neutral-300">
            {state?.problem_model.current_focus || state?.problem_model.purpose || 'No problem model focus yet.'}
          </p>
        </div>
        <div className="mt-3 border-t border-neutral-800 pt-2">
          <StatRow label="active gaps" value={state?.problem_model.active_gap_count ?? 0} />
          <StatRow label="capabilities" value={state?.problem_model.capability_count ?? 0} />
          <StatRow label="needs answer" value={state?.problem_model.pending_confirmation_count ?? 0} />
        </div>
        {state?.problem_model.top_gap && (
          <p className="mt-2 line-clamp-2 text-xs leading-relaxed text-neutral-500">
            {state.problem_model.top_gap.name}
          </p>
        )}
        <div className="mt-3">
          <RailButton onClick={() => onOpenProjectSection('problem-model')}>Model</RailButton>
        </div>
      </WorkbenchPanel>

      <WorkbenchPanel title="Direction" meta={`${state?.directions.active_count ?? 0} active`}>
        <div className="flex min-w-0 gap-2">
          <GitBranch className="mt-0.5 h-4 w-4 shrink-0 text-neutral-500" aria-hidden="true" />
          <p className="line-clamp-3 text-xs leading-relaxed text-neutral-300">
            {state?.directions.primary?.text || 'No active direction selected.'}
          </p>
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          <RailButton onClick={() => onOpenProjectSection('directions')}>Directions</RailButton>
          <RailButton onClick={() => onOpenProjectSection('running-update')}>Update</RailButton>
        </div>
      </WorkbenchPanel>

      <WorkbenchPanel title="Context" meta={formatCost(state?.tasks.cumulative_estimated_cost_usd)}>
        <div className="space-y-3">
          <ContextPackageBlock
            contextPackage={state?.context_package}
            sessionRefs={selectedRefDescriptors}
            selectedRefs={selectedRefs}
            savingSessionRefs={savingSessionRefs}
            onOpenResource={openContextResource}
            onOpenTarget={openContextTarget}
            onUseRef={onUpdateSessionRefs ? useSessionRef : undefined}
            onRemoveRef={onUpdateSessionRefs ? removeSessionRef : undefined}
          />
          <div>
            <div className="mb-1 flex items-center gap-2 text-xs font-semibold text-neutral-100">
              <FileText className="h-4 w-4 text-neutral-500" aria-hidden="true" />
              Running update
            </div>
            <p className="line-clamp-3 text-xs leading-relaxed text-neutral-400">
              {state?.running_update.excerpt || 'No running update yet.'}
            </p>
          </div>
          <div className="border-t border-neutral-800 pt-2">
            <StatRow label="running" value={taskCounts?.running ?? 0} />
            <StatRow label="queued" value={taskCounts?.queued ?? 0} />
            <StatRow label="blocked" value={taskCounts?.blocked ?? 0} />
            <StatRow label="parked" value={taskCounts?.parked ?? 0} />
            <StatRow label="needs operator" value={attentionNeeds} />
            <StatRow label="watch" value={attentionWatch} />
            <StatRow label="linked messages" value={state?.messages.counts.linked ?? 0} />
          </div>
          <div className="flex flex-wrap gap-2">
            <RailButton onClick={onOpenPipeline}>Pipeline</RailButton>
            <RailButton onClick={onOpenRuntimeHealth}>Runtime</RailButton>
            {state?.design.has_design_doc && (
              <RailButton onClick={() => onOpenProjectSection('design-doc')}>Design</RailButton>
            )}
          </div>
        </div>
      </WorkbenchPanel>
    </div>
  );
}
