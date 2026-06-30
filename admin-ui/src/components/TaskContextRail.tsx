import type { ReactNode } from 'react';
import { Activity, ArrowUpRight, BookmarkMinus, BookmarkPlus, GitPullRequest, ListTree, Radio, Route, Star } from 'lucide-react';
import type { StrategicWorkbenchState } from '../strategicStateApi';
import type { Task } from '../types/task';
import { timeAgo } from '../utils/time';
import { removeStrategicSelectedRef, upsertStrategicSelectedRef } from '../utils/strategicWorkbenchSession';
import { WorkbenchMetric, WorkbenchPanel } from './WorkbenchLayout';
import { StatusBadge, TaskTypeBadge } from './TaskCard';

interface TaskContextRailProps {
  currentTask?: Task | null;
  attentionTasks?: Task[];
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

function attentionTone(task: Task): string {
  if (task.status === 'FAILED') return 'border-red-500/30 bg-red-500/5';
  if (task.status === 'BLOCKED' || task.status === 'PARKED') return 'border-amber-500/30 bg-amber-500/5';
  return 'border-neutral-800 bg-neutral-900/40';
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
      className={`inline-flex min-h-[44px] items-center gap-1.5 rounded-md border border-neutral-800 bg-neutral-950/70 px-2.5 text-xs font-semibold text-neutral-300 transition-colors ${disabled ? 'cursor-not-allowed opacity-50' : 'hover:border-neutral-700 hover:bg-neutral-900 hover:text-neutral-100'}`}
    >
      {children}
    </button>
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
  const dependencyCount = task.depends_on?.length ?? 0;
  const childCount = task.sideboard_context?.pipeline.child_count ?? task.child_task_ids?.length ?? 0;
  const direction = task.sideboard_context?.strategic.direction;
  const prUrl = task.pr_url ?? task.sideboard_context?.artifacts.primary_pr_url ?? null;

  return (
    <div className="space-y-3">
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

      <div className="grid grid-cols-2 gap-2">
        <WorkbenchMetric label={task.started_at ? 'running' : 'created'} value={taskAge(task)} />
        <WorkbenchMetric label="cost" value={costLabel(task) ?? 'n/a'} />
      </div>

      {(dependencyCount > 0 || childCount > 0 || direction) && (
        <div className="space-y-1.5 border-t border-neutral-800 pt-2 text-xs text-neutral-400">
          {direction && (
            <div className="flex min-w-0 gap-2">
              <Route className="mt-0.5 h-3.5 w-3.5 shrink-0 text-neutral-600" aria-hidden="true" />
              <span className="line-clamp-2">{direction.text || direction.id}</span>
            </div>
          )}
          {dependencyCount > 0 && (
            <div className="flex items-center gap-2">
              <ListTree className="h-3.5 w-3.5 text-neutral-600" aria-hidden="true" />
              <span>waiting on {dependencyCount}</span>
            </div>
          )}
          {childCount > 0 && (
            <div className="flex items-center gap-2">
              <Activity className="h-3.5 w-3.5 text-neutral-600" aria-hidden="true" />
              <span>{childCount} child task{childCount === 1 ? '' : 's'}</span>
            </div>
          )}
        </div>
      )}

      {task.latest_status_message && (
        <p className="line-clamp-3 rounded-md border border-neutral-800 bg-neutral-900/40 p-2 text-xs leading-relaxed text-neutral-400">
          {task.latest_status_message}
        </p>
      )}

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
            className="inline-flex min-h-[44px] items-center gap-1.5 rounded-md border border-neutral-800 bg-neutral-950/70 px-2.5 text-xs font-semibold text-neutral-300 transition-colors hover:border-neutral-700 hover:bg-neutral-900 hover:text-neutral-100"
          >
            <GitPullRequest className="h-3.5 w-3.5 text-neutral-500" aria-hidden="true" />
            PR
          </a>
        )}
      </div>
    </div>
  );
}

function AttentionTaskRow({ task, onOpenTask }: { task: Task; onOpenTask: (task: Task) => void }) {
  return (
    <button
      type="button"
      onClick={() => onOpenTask(task)}
      className={`block w-full rounded-md border px-3 py-2 text-left transition-colors hover:border-neutral-600 hover:bg-neutral-900 ${attentionTone(task)}`}
    >
      <div className="flex min-w-0 items-center justify-between gap-2">
        <span className="truncate text-ui-sm font-medium uppercase text-neutral-500">{task.status}</span>
        <span className="shrink-0 font-mono text-ui-sm text-neutral-600">{task.id.slice(0, 8)}</span>
      </div>
      <div className="mt-1 truncate text-xs font-medium text-neutral-200">{task.title || task.id}</div>
      {task.latest_status_message && (
        <div className="mt-0.5 truncate text-ui-sm text-neutral-500">{task.latest_status_message}</div>
      )}
    </button>
  );
}

function AttentionTaskList({
  tasks,
  onOpenTask,
}: {
  tasks: Task[];
  onOpenTask: (task: Task) => void;
}) {
  return (
    <div className="space-y-2">
      {tasks.map(task => (
        <AttentionTaskRow key={task.id} task={task} onOpenTask={onOpenTask} />
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
  const backendIds = new Set(attentionTasks.map(t => t.id));
  const mergedAttention = [...attentionTasks, ...localFallbackTasks.filter(t => !backendIds.has(t.id))];
  const visibleAttention = mergedAttention
    .filter(task => task.id !== currentTask?.id)
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
