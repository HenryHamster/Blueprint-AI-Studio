import type { ReactNode } from 'react';
import { ArrowUpRight, CircleAlert, Cpu, FileText, GitBranch, Network, Radio } from 'lucide-react';
import type { StrategicCycleCompletion, StrategicCycleProgress } from '../hooks/useLogViewer';
import type {
  StrategicContextPackageDescriptor,
  StrategicLocalRunnerState,
  StrategicStateAttentionItem,
  StrategicStateResponse,
} from '../strategicStateApi';
import { WorkbenchPanel } from './WorkbenchLayout';

type ProjectTarget = 'running-update' | 'directions' | 'problem-model' | 'design-doc';

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
  onOpenTask?: (taskId: string) => void;
  onOpenMessages?: () => void;
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

function ContextPackageBlock({
  contextPackage,
}: {
  contextPackage?: StrategicContextPackageDescriptor | null;
}) {
  if (!contextPackage) return null;
  const tone = contextPackageTone(contextPackage.status);
  const sources = (contextPackage.sources ?? []).slice(0, 5);
  const itemCount = Number(contextPackage.item_count ?? 0);

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
  onOpenTask,
  onOpenMessages,
}: StrategicContextRailProps) {
  const activeCycle = state?.strategic.active_cycle ?? null;
  const localRunner = state?.runtime?.local_runner ?? null;
  const pendingSteer = state?.strategic.refresh.pending ?? null;
  const latestCycle = state?.strategic.latest_cycle ?? null;
  const taskCounts = state?.tasks.counts;
  const attention = state?.attention ?? null;
  const attentionItems = attention?.items.slice(0, 2) ?? [];
  const attentionNeeds = attention?.counts.needs_operator ?? 0;
  const attentionWatch = attention?.counts.watch ?? 0;
  const staleCycle = Boolean(activeCycle?.stale);
  const operatorSteerQueued = Boolean(
    pendingSteer && (pendingSteer.origin === 'operator' || (pendingSteer.operator_count ?? 0) > 0),
  );
  const isWorking = !staleCycle && (strategistActive || Boolean(activeCycle?.has_active_task));
  const statusTone = isWorking ? 'bg-cyan-400' : pendingSteer ? 'bg-amber-300' : 'bg-neutral-600';
  const statusLabel = isWorking
    ? (cycleProgress?.phase_label ?? phaseLabel(state?.strategic.active_phase))
    : staleCycle
      ? 'Cycle stale'
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

  function attentionItemAction(item: StrategicStateAttentionItem): (() => void) | undefined {
    if (item.kind === 'task') {
      return onOpenTask && item.ref?.id ? () => onOpenTask(item.ref.id) : onOpenPipeline;
    }
    if (item.kind === 'direction') return () => onOpenProjectSection('directions');
    if (item.kind === 'alert' && item.task_id) return onOpenPipeline;
    if (item.kind === 'alert') return onOpenRuntimeHealth;
    if (item.kind === 'message') return onOpenMessages ?? onOpenPipeline;
    return undefined;
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
      </WorkbenchPanel>

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
