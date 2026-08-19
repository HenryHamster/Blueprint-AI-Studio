import type { ReactNode } from 'react';
import { ArrowUpRight, BookmarkMinus, BookmarkPlus, GitPullRequest, ListTree, Radio, Route, Star } from 'lucide-react';
import type { StrategicStateAttentionItem, StrategicWorkbenchState } from '../strategicStateApi';
import type { Task } from '../types/task';
import { timeAgo } from '../utils/time';
import { safeExternalUrl } from '../utils/externalUrl';
import { removeStrategicSelectedRef, upsertStrategicSelectedRef } from '../utils/strategicWorkbenchSession';
import { WorkbenchPanel } from './WorkbenchLayout';
import { StatusBadge, TaskTypeBadge } from './TaskCard';

interface TaskContextRailProps {
  currentTask?: Task | null;
  attentionTasks?: AttentionTaskInput[];
  localFallbackTasks?: Task[];
  trackedTaskIds?: Set<string>;
  onOpenTask: (task: Task) => void;
  onOpenPipeline?: () => void;
  onTrackTask?: (task: Task) => void;
  sessionRefs?: StrategicWorkbenchState['selected_refs'];
  savingSessionRefs?: boolean;
  onUpdateSessionRefs?: (
    updateRefs: (refs: StrategicWorkbenchState['selected_refs']) => StrategicWorkbenchState['selected_refs']
  ) => void;
}

export interface AttentionTaskEntry {
  task: Task;
  attention?: StrategicStateAttentionItem;
}

export type AttentionTaskInput = Task | AttentionTaskEntry;

type StrategicSelectedRef = StrategicWorkbenchState['selected_refs'][number];

function taskType(task: Task): Task['task_type'] {
  return (task.task_type ?? ((task.payloads?.[0] as Record<string, unknown> | undefined)?.type as Task['task_type'] | undefined) ?? 'create') as Task['task_type'];
}

function taskAge(task: Task): string {
  const start = task.started_at ?? task.created_at;
  return start ? timeAgo(start) : 'unknown';
}

function costLabel(task: Task): string | null {
  if (task.estimated_cost_usd == null) return null;
  return `$${task.estimated_cost_usd.toFixed(2)}`;
}

type TaskRailFact = {
  key: string;
  label: string;
  value: string;
  title?: string;
};

type ApiCallSummaryWithExtras = NonNullable<Task['api_call_summary']> & {
  total_calls?: unknown;
  provider?: unknown;
  model?: unknown;
  by_model?: Array<{ provider?: unknown; model?: unknown }> | null;
};

function stringValue(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function numberValue(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function countLabel(count: number, singular: string): string {
  return `${count.toLocaleString()} ${singular}${count === 1 ? '' : 's'}`;
}

function compactIdentifier(value: string): string {
  return value.length > 24 ? `${value.slice(0, 12)}...${value.slice(-6)}` : value;
}

function contextString(task: Task, key: string): string | null {
  return stringValue((task.context as Record<string, unknown> | undefined)?.[key]);
}

function taskRunnerFact(task: Task): TaskRailFact | null {
  const parts: string[] = [];
  const titleParts: string[] = [];
  const vmInstanceId = stringValue(task.vm_instance_id);
  const runnerTargetId = contextString(task, 'runner_target_id');

  if (task.vm_machine_type) parts.push(task.vm_machine_type);
  if (task.vm_warm_start === true) parts.push('warm start');
  if (task.vm_spot === true) parts.push('spot');
  if (vmInstanceId) {
    parts.push(`vm ${compactIdentifier(vmInstanceId)}`);
    titleParts.push(`vm ${vmInstanceId}`);
  }
  if (runnerTargetId) {
    parts.push(`runner ${compactIdentifier(runnerTargetId)}`);
    titleParts.push(`runner ${runnerTargetId}`);
  }

  return parts.length > 0
    ? { key: 'runner', label: 'runner', value: parts.join(' · '), title: titleParts.join(' · ') || undefined }
    : null;
}

function taskLlmFact(task: Task): TaskRailFact | null {
  const summary = task.api_call_summary as ApiCallSummaryWithExtras | null | undefined;
  const modelRows = Array.isArray(summary?.by_model) ? summary.by_model : [];
  const firstModelRow = modelRows.find(row => stringValue(row.provider) || stringValue(row.model));
  const provider = stringValue(summary?.provider) ?? stringValue(firstModelRow?.provider) ?? contextString(task, 'llm_provider');
  const model = stringValue(summary?.model) ?? stringValue(firstModelRow?.model) ?? contextString(task, 'llm_model');
  const calls = numberValue(summary?.total_calls) ?? numberValue(summary?.llm_call_count);
  const parts: string[] = [];

  if (task.task_llm_cost_usd != null && task.task_llm_cost_usd > 0) {
    parts.push(`${task.task_llm_cost_any_unpriced ? '~' : ''}$${task.task_llm_cost_usd.toFixed(4)}`);
  }
  if (calls != null && calls > 0) parts.push(countLabel(calls, 'call'));
  if (provider || model) parts.push(provider && model ? `${provider}/${model}` : provider ?? model ?? '');

  return parts.length > 0 ? { key: 'llm', label: 'llm', value: parts.join(' · ') } : null;
}

function taskSideboardFact(task: Task): TaskRailFact | null {
  const context = task.sideboard_context;
  if (!context) return null;

  const artifactCount = numberValue(context.artifacts?.total);
  const recentMessageCount = numberValue(context.messages?.total_recent);
  const parts = [
    stringValue(context.purpose),
    artifactCount != null && artifactCount > 0 ? countLabel(artifactCount, 'artifact') : null,
    recentMessageCount != null && recentMessageCount > 0 ? countLabel(recentMessageCount, 'message') : null,
  ].filter((part): part is string => Boolean(part));

  return parts.length > 0 ? { key: 'sideboard', label: 'context', value: parts.join(' · ') } : null;
}

function taskRailFacts(task: Task): TaskRailFact[] {
  return [
    task.last_heartbeat ? { key: 'heartbeat', label: 'heartbeat', value: timeAgo(task.last_heartbeat) } : null,
    task.last_tool_use_at ? { key: 'tool', label: 'tool use', value: timeAgo(task.last_tool_use_at) } : null,
    taskRunnerFact(task),
    taskLlmFact(task),
    taskSideboardFact(task),
  ].filter((fact): fact is TaskRailFact => Boolean(fact));
}

function attentionTone(task: Task): string {
  if (task.status === 'FAILED') return 'border-red-500/30 bg-red-500/5';
  if (task.status === 'BLOCKED' || task.status === 'PARKED') return 'border-amber-500/30 bg-amber-500/5';
  return 'border-neutral-800 bg-workbench-base/40';
}

function TaskRailButton({
  children,
  onClick,
  title,
  disabled = false,
}: {
  children: ReactNode;
  onClick: () => void;
  title?: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      disabled={disabled}
      className={`inline-flex min-h-[44px] items-center gap-1.5 rounded-md border border-neutral-800 bg-workbench-shell/70 px-2.5 text-xs font-semibold text-neutral-300 transition-colors ${disabled ? 'cursor-not-allowed opacity-50' : 'hover:border-neutral-700 hover:bg-workbench-hover hover:text-neutral-100'}`}
    >
      {children}
    </button>
  );
}

function TaskSnapshotMetric({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="min-w-0 rounded-md border border-neutral-800 bg-workbench-hover px-2.5 py-2">
      <div className="text-ui-sm font-semibold uppercase text-neutral-600">{label}</div>
      <div className="mt-0.5 min-w-0 truncate text-sm font-semibold text-neutral-100">{value}</div>
    </div>
  );
}

function TaskFactRows({ facts }: { facts: TaskRailFact[] }) {
  if (facts.length === 0) return null;

  return (
    <dl className="min-w-0 overflow-hidden rounded-md border border-neutral-800 bg-workbench-shell/45">
      {facts.map(fact => (
        <div
          key={fact.key}
          data-testid={`task-rail-fact-${fact.key}`}
          className="grid min-w-0 grid-cols-[5.4rem_minmax(0,1fr)] gap-2 border-b border-neutral-800/80 bg-workbench-layer/70 px-2 py-1.5 last:border-b-0"
        >
          <dt className="text-ui-sm font-semibold uppercase text-neutral-600">{fact.label}</dt>
          <dd className="min-w-0 break-words text-neutral-300" title={fact.title}>{fact.value}</dd>
        </div>
      ))}
    </dl>
  );
}

function PipelineContextRows({
  parentTaskId,
  dependencyCount,
  childCount,
  blockedReason,
}: {
  parentTaskId?: string | null;
  dependencyCount: number;
  childCount: number;
  blockedReason?: string | null;
}) {
  if (!parentTaskId && dependencyCount === 0 && childCount === 0 && !blockedReason) return null;

  return (
    <div className="min-w-0 overflow-hidden rounded-md border border-neutral-800 bg-workbench-shell/45">
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 bg-workbench-layer/70 px-2 py-1.5">
        <ListTree className="h-3.5 w-3.5 shrink-0 text-neutral-600" aria-hidden="true" />
        {parentTaskId && (
          <span className="min-w-0 break-all font-mono text-neutral-300">parent {compactIdentifier(parentTaskId)}</span>
        )}
        {dependencyCount > 0 && <span className="text-neutral-400">waiting on {dependencyCount}</span>}
        {childCount > 0 && (
          <span className="text-neutral-400">{countLabel(childCount, 'child task')}</span>
        )}
      </div>
      {blockedReason && (
        <div className="border-t border-rose-500/25 bg-rose-500/5 px-2 py-1.5 text-rose-200">
          <span className="font-semibold uppercase text-rose-400/80">Blocked </span>
          <span className="break-words">{blockedReason}</span>
        </div>
      )}
    </div>
  );
}

function taskSessionRef(task: Task): StrategicSelectedRef {
  return { kind: 'task', id: task.id, label: task.title || task.id };
}

function CurrentTaskCard({
  task,
  tracked,
  onOpenTask,
  onOpenPipeline,
  onTrackTask,
  inSessionContext,
  savingSessionRefs,
  onKeepContext,
  onRemoveContext,
}: {
  task: Task;
  tracked: boolean;
  onOpenTask: (task: Task) => void;
  onOpenPipeline?: () => void;
  onTrackTask?: (task: Task) => void;
  inSessionContext?: boolean;
  savingSessionRefs?: boolean;
  onKeepContext?: () => void;
  onRemoveContext?: () => void;
}) {
  const dependencyCount = task.sideboard_context?.pipeline.dependency_count ?? task.depends_on?.length ?? 0;
  const childCount = task.sideboard_context?.pipeline.child_count ?? task.child_task_ids?.length ?? 0;
  const parentTaskId = task.sideboard_context?.pipeline.parent_task_id ?? null;
  const blockedReason = task.sideboard_context?.pipeline.blocked_reason ?? null;
  const direction = task.sideboard_context?.strategic.direction;
  const prUrl = safeExternalUrl(task.pr_url ?? task.sideboard_context?.artifacts.primary_pr_url ?? null);
  const facts = taskRailFacts(task);

  return (
    <div className="space-y-3">
      <div className="rounded-md border border-neutral-800 bg-[#0d141c] p-2.5">
        <button
          type="button"
          onClick={() => onOpenTask(task)}
          className="block w-full min-w-0 text-left"
        >
          <div className="mb-2 flex min-w-0 items-center gap-2">
            <StatusBadge status={task.status} />
            <TaskTypeBadge taskType={taskType(task)} />
          </div>
          <div className="line-clamp-2 text-sm font-semibold leading-snug text-neutral-100 hover:text-cyan-300">
            {task.title || task.id}
          </div>
        </button>

        {task.latest_status_message && (
          <div className="mt-2 border-t border-neutral-800 pt-2">
            <div className="mb-1 text-ui-sm font-semibold uppercase text-neutral-600">Latest status</div>
            <p className="line-clamp-3 text-xs leading-relaxed text-neutral-400">
              {task.latest_status_message}
            </p>
          </div>
        )}
      </div>

      <div className="grid grid-cols-2 gap-1.5">
        <TaskSnapshotMetric label={task.started_at ? 'running' : 'created'} value={taskAge(task)} />
        <TaskSnapshotMetric label="cost" value={costLabel(task) ?? 'n/a'} />
      </div>

      {(facts.length > 0 || dependencyCount > 0 || childCount > 0 || parentTaskId || blockedReason || direction) && (
        <div className="space-y-2 text-xs text-neutral-400">
          <TaskFactRows facts={facts} />
          {direction && (
            <div className="flex min-w-0 gap-2 rounded-md border border-neutral-800 bg-workbench-shell/45 px-2 py-1.5">
              <Route className="mt-0.5 h-3.5 w-3.5 shrink-0 text-neutral-600" aria-hidden="true" />
              <span className="line-clamp-2">{direction.text || direction.id}</span>
            </div>
          )}
          <PipelineContextRows
            parentTaskId={parentTaskId}
            dependencyCount={dependencyCount}
            childCount={childCount}
            blockedReason={blockedReason}
          />
        </div>
      )}

      <div className="border-t border-neutral-800 pt-2">
        <div className="mb-1.5 text-ui-sm font-semibold uppercase text-neutral-600">Task actions</div>
        <div className="flex flex-wrap gap-2">
          <TaskRailButton onClick={() => onOpenTask(task)}>
            Details
            <ArrowUpRight className="h-3.5 w-3.5 text-neutral-500" aria-hidden="true" />
          </TaskRailButton>
          {onOpenPipeline && (
            <TaskRailButton onClick={onOpenPipeline}>
              Pipeline
            </TaskRailButton>
          )}
          {onTrackTask && !tracked && (
            <TaskRailButton onClick={() => onTrackTask(task)}>
              <Star className="h-3.5 w-3.5 text-neutral-500" aria-hidden="true" />
              Track
            </TaskRailButton>
          )}
          {onKeepContext && !inSessionContext && (
            <TaskRailButton onClick={onKeepContext} disabled={savingSessionRefs} title="Keep task in Strategic context">
              <BookmarkPlus className="h-3.5 w-3.5 text-neutral-500" aria-hidden="true" />
              Keep context
            </TaskRailButton>
          )}
          {onRemoveContext && inSessionContext && (
            <TaskRailButton onClick={onRemoveContext} disabled={savingSessionRefs} title="Remove task from Strategic context">
              <BookmarkMinus className="h-3.5 w-3.5 text-neutral-500" aria-hidden="true" />
              Remove context
            </TaskRailButton>
          )}
          {prUrl && (
            <a
              href={prUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex min-h-[44px] items-center gap-1.5 rounded-md border border-neutral-800 bg-workbench-shell/70 px-2.5 text-xs font-semibold text-neutral-300 transition-colors hover:border-neutral-700 hover:bg-workbench-hover hover:text-neutral-100"
            >
              <GitPullRequest className="h-3.5 w-3.5 text-neutral-500" aria-hidden="true" />
              PR
            </a>
          )}
        </div>
      </div>
    </div>
  );
}

function normalizeAttentionTask(entry: AttentionTaskInput): AttentionTaskEntry {
  if ('task' in entry) return entry;
  return { task: entry };
}

function AttentionTaskRow({ entry, onOpenTask }: { entry: AttentionTaskEntry; onOpenTask: (task: Task) => void }) {
  const { task, attention } = entry;
  const eyebrow = attention?.label || task.status;
  const detail = attention?.detail || task.latest_status_message;
  return (
    <button
      type="button"
      onClick={() => onOpenTask(task)}
      className={`block w-full rounded-md border px-3 py-2 text-left transition-colors hover:border-neutral-600 hover:bg-workbench-hover ${attentionTone(task)}`}
    >
      <div className="flex min-w-0 items-center justify-between gap-2">
        <span className="truncate text-ui-sm font-medium uppercase text-neutral-500" title={eyebrow}>{eyebrow}</span>
        <span className="shrink-0 font-mono text-ui-sm text-neutral-600">{task.id.slice(0, 8)}</span>
      </div>
      <div className="mt-1 truncate text-xs font-medium text-neutral-200">{task.title || task.id}</div>
      {detail && (
        <div className="mt-0.5 truncate text-ui-sm text-neutral-500" title={detail}>{detail}</div>
      )}
    </button>
  );
}

function AttentionTaskList({
  tasks,
  onOpenTask,
}: {
  tasks: AttentionTaskEntry[];
  onOpenTask: (task: Task) => void;
}) {
  return (
    <div className="space-y-2">
      {tasks.map(entry => (
        <AttentionTaskRow key={entry.task.id} entry={entry} onOpenTask={onOpenTask} />
      ))}
    </div>
  );
}

export function TaskContextRail({
  currentTask = null,
  attentionTasks = [],
  localFallbackTasks = [],
  trackedTaskIds,
  onOpenTask,
  onOpenPipeline,
  onTrackTask,
  sessionRefs,
  savingSessionRefs = false,
  onUpdateSessionRefs,
}: TaskContextRailProps) {
  const backendAttention = attentionTasks.map(normalizeAttentionTask);
  const backendIds = new Set(backendAttention.map(entry => entry.task.id));
  const mergedAttention = [
    ...backendAttention,
    ...localFallbackTasks
      .filter(task => !backendIds.has(task.id))
      .map(task => ({ task })),
  ];
  const visibleAttention = mergedAttention
    .filter(entry => entry.task.id !== currentTask?.id)
    .slice(0, currentTask ? 3 : 5);
  const tracked = Boolean(currentTask && trackedTaskIds?.has(currentTask.id));
  const currentTaskRef = currentTask ? taskSessionRef(currentTask) : null;
  const sessionRefsReady = Array.isArray(sessionRefs);
  const currentTaskInSession = Boolean(currentTaskRef && sessionRefsReady && sessionRefs.some(ref => ref.kind === currentTaskRef.kind && ref.id === currentTaskRef.id));
  const keepCurrentTaskContext = currentTaskRef && sessionRefsReady && onUpdateSessionRefs
    ? () => onUpdateSessionRefs(refs => upsertStrategicSelectedRef(refs, currentTaskRef))
    : undefined;
  const removeCurrentTaskContext = currentTaskRef && sessionRefsReady && onUpdateSessionRefs
    ? () => onUpdateSessionRefs(refs => removeStrategicSelectedRef(refs, currentTaskRef))
    : undefined;

  return (
    <WorkbenchPanel title="Task context" meta={currentTask ? currentTask.status : visibleAttention.length || 'clear'}>
      {currentTask ? (
        <div className="space-y-4">
          <CurrentTaskCard
            task={currentTask}
            tracked={tracked}
            onOpenTask={onOpenTask}
            onOpenPipeline={onOpenPipeline}
            onTrackTask={onTrackTask}
            inSessionContext={currentTaskInSession}
            savingSessionRefs={savingSessionRefs}
            onKeepContext={keepCurrentTaskContext}
            onRemoveContext={removeCurrentTaskContext}
          />
          {visibleAttention.length > 0 && (
            <div className="border-t border-neutral-800 pt-3">
              <div className="mb-2 text-ui-sm font-semibold uppercase text-neutral-500">Needs attention</div>
              <AttentionTaskList tasks={visibleAttention} onOpenTask={onOpenTask} />
            </div>
          )}
        </div>
      ) : visibleAttention.length > 0 ? (
        <div className="space-y-3">
          <AttentionTaskList tasks={visibleAttention} onOpenTask={onOpenTask} />
          {onOpenPipeline && (
            <div className="pt-1">
              <TaskRailButton onClick={onOpenPipeline}>Open pipeline</TaskRailButton>
            </div>
          )}
        </div>
      ) : (
        <div className="flex items-center gap-2 py-5 text-xs text-neutral-600">
          <Radio className="h-4 w-4" aria-hidden="true" />
          <span>No task selected.</span>
        </div>
      )}
    </WorkbenchPanel>
  );
}
