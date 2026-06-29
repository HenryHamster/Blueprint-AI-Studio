import type { ReactNode } from 'react';
import { ArrowUpRight, FileText, GitBranch, Radio } from 'lucide-react';
import type { StrategicCycleCompletion, StrategicCycleProgress } from '../hooks/useLogViewer';
import type { StrategicStateResponse } from '../strategicStateApi';
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
}

function formatCost(value: number | undefined): string {
  const n = Number.isFinite(value) ? Number(value) : 0;
  return `$${n.toFixed(2)}`;
}

function phaseLabel(phase?: string | null): string {
  if (phase === 'strategic_review') return 'Strategic review';
  if (phase === 'design_doc_update') return 'Architecture review';
  return 'Strategic';
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
}: StrategicContextRailProps) {
  const activeCycle = state?.strategic.active_cycle ?? null;
  const pendingSteer = state?.strategic.refresh.pending ?? null;
  const latestCycle = state?.strategic.latest_cycle ?? null;
  const taskCounts = state?.tasks.counts;
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
