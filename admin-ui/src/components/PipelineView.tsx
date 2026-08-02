import { useMemo, useState } from 'react';
import { AlertTriangle, ChevronDown, ChevronRight, ExternalLink, Archive } from 'lucide-react';
import type { Task } from './TaskCard';
import { TASK_TYPE_STYLES, useElapsedTime, getHeartbeatStatus } from './TaskCard';
import { parseTimestamp } from '../utils/time';
import { getSidecarBackref, deriveSidecarBadge, SIDECAR_TONE_CLASSES, type SidecarBadge } from '../utils/sidecarTasks';
import { buildForest, type TreeNode } from './PipelineView.utils';
import { taskStatusDotColor } from '../utils/taskStatus';

const ACTIVE_STATUSES = ['IN_PROGRESS', 'PROVISIONING', 'EVALUATING'];
const TERMINAL_STATUSES = ['COMPLETED', 'FAILED', 'CANCELLED'];

interface PipelineViewProps {
  tasks: Task[];
  loading?: boolean;
  hasActiveFilters?: boolean;
  onViewLogs: (task: Task) => void;
  onNavigateToParent?: (parentTaskId: string) => void;
  onClearWaveDeps?: () => void;
  onArchiveGroup?: (taskIds: string[]) => void;
  onUnarchiveTask?: (taskId: string) => void;
}

function TaskNode({ task, onViewLogs, isOrphan, onNavigateToParent, onUnarchiveParent, blockingCount, sidecarBadges, isSidecar, isSidecarFallback }: { task: Task; onViewLogs: (task: Task) => void; isOrphan?: boolean; onNavigateToParent?: (parentTaskId: string) => void; onUnarchiveParent?: (parentTaskId: string) => void; blockingCount?: number; sidecarBadges?: SidecarBadge[]; isSidecar?: boolean; isSidecarFallback?: boolean }) {
  const style = task.task_type ? TASK_TYPE_STYLES[task.task_type] : null;
  const Icon = style?.icon;
  const statusColor = taskStatusDotColor(task.status);
  const isRunning = task.status === 'IN_PROGRESS' || task.status === 'PROVISIONING';
  const heartbeat = isRunning && task.last_heartbeat ? getHeartbeatStatus(task.last_heartbeat) : null;

  const isActiveForDuration = task.status === 'IN_PROGRESS' || task.status === 'PROVISIONING';
  const isTerminal = task.status === 'COMPLETED' || task.status === 'FAILED' || task.status === 'CANCELLED';
  const endTime = isTerminal && task.history && task.history.length > 0
    ? task.history[task.history.length - 1].timestamp
    : isTerminal ? task.created_at : undefined;
  const duration = useElapsedTime(task.created_at, isActiveForDuration ? undefined : endTime);
  // Surface the durable blocker metadata (backend models.py) on the pipeline node
  // tooltip — the only board view that renders its own compact card instead of
  // <TaskCard> (which already shows these). All fields are nullable and absent on
  // non-BLOCKED / legacy tasks, so guard on presence before adding a line.
  const blockerTooltip = task.status === 'BLOCKED'
    ? [
        task.blocker_kind ? `Blocked: ${task.blocker_kind}` : 'Blocked',
        task.blocked_reason || null,
        task.recheck_after ? `Rechecks ${new Date(parseTimestamp(task.recheck_after).getTime()).toLocaleString()}` : null,
      ].filter(Boolean).join('\n')
    : null;

  return (
    <div
      role="button"
      tabIndex={0}
      data-task-id={task.id}
      onClick={() => onViewLogs(task)}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onViewLogs(task); } }}
      className={`flex min-h-[44px] w-full min-w-0 cursor-pointer items-center gap-2 rounded-md border bg-[#10151c] px-2.5 py-1.5 text-left transition-colors hover:border-neutral-600 hover:bg-neutral-900 md:min-h-0 group ${isOrphan ? 'border-dashed border-neutral-600' : 'border-neutral-800'}`}
      title={`${task.title}\n${task.status} • ${task.task_type || 'create'}${blockerTooltip ? `\n${blockerTooltip}` : ''}\nClick to view logs`}
    >
      {/* Status dot */}
      <div className={`h-2 w-2 shrink-0 rounded-full ${statusColor}`} />
      {heartbeat && (
        <div className={`w-1.5 h-1.5 rounded-full shrink-0 ${heartbeat.color}${heartbeat.color === 'bg-red-500' ? ' animate-pulse' : ''}`} title={heartbeat.text} />
      )}

      {/* Orphan indicator — the real parent is either archived or on another
          page. Two actions: copy parent id (existing) + unarchive-parent
          (new) so users can pull an archived parent back into view without
          hunting for it in the Archive panel. */}
      {isOrphan && task.parent_task_id && (() => {
        const parentId = task.parent_task_id;
        return (
        <>
          <button
            onClick={e => { e.stopPropagation(); onNavigateToParent?.(parentId); }}
            className="min-h-[44px] min-w-[44px] inline-flex items-center justify-center text-ui-xs text-amber-500/80 border border-amber-500/30 bg-amber-500/10 hover:bg-amber-500/20 hover:text-amber-400 px-1.5 rounded font-medium whitespace-nowrap shrink-0 transition-colors cursor-pointer"
            title={`Copy parent task ID: ${parentId}`}
          >
            ↑ Parent on another page
          </button>
          {onUnarchiveParent && (
            <button
              onClick={e => { e.stopPropagation(); onUnarchiveParent(parentId); }}
              className="min-h-[44px] min-w-[44px] inline-flex items-center justify-center text-ui-xs text-emerald-500/80 border border-emerald-500/30 bg-emerald-500/10 hover:bg-emerald-500/20 hover:text-emerald-400 px-1.5 rounded font-medium whitespace-nowrap shrink-0 transition-colors cursor-pointer"
              title="Unarchive parent task (idempotent if already unarchived)"
            >
              ↪ Unarchive parent
            </button>
          )}
        </>
        );
      })()}

      {/* Side-car indicator — this row IS a side-car (grader/challenger). The
          fallback variant means its primary task is not on this page/filter,
          so the row renders top-level with a navigate-to-primary chip. */}
      {(isSidecar || isSidecarFallback) && (() => {
        const backref = getSidecarBackref(task);
        if (!backref) return null;
        return (
          <>
            <span
              className="text-ui-xs text-violet-400 bg-violet-500/10 border border-violet-500/30 px-1.5 py-0.5 rounded font-semibold uppercase tracking-wider whitespace-nowrap shrink-0"
              title={`Side-car (${backref.kind}) for task ${backref.primaryTaskId}`}
            >
              ↳ side-car
            </span>
            {isSidecarFallback && (
              <button
                onClick={e => { e.stopPropagation(); onNavigateToParent?.(backref.primaryTaskId); }}
                className="min-h-[44px] min-w-[44px] inline-flex items-center justify-center text-ui-xs text-amber-500/80 border border-amber-500/30 bg-amber-500/10 hover:bg-amber-500/20 hover:text-amber-400 px-1.5 rounded font-medium whitespace-nowrap shrink-0 transition-colors cursor-pointer"
                title={`Copy primary task ID: ${backref.primaryTaskId}`}
              >
                ↑ Primary on another page
              </button>
            )}
          </>
        );
      })()}

      {/* Type badge */}
      {style && Icon && (
        <span className={`${style.bg} ${style.text} border text-ui-xs uppercase tracking-wider px-1.5 py-0.5 rounded font-semibold whitespace-nowrap flex items-center gap-0.5 shrink-0`}>
          <Icon className="w-2.5 h-2.5" />{style.label}
        </span>
      )}

      {/* Retry badge */}
      {task.attempt_count != null && task.attempt_count > 0 && (
        <span className="border border-violet-500/20 bg-violet-500/10 px-1.5 py-0.5 text-ui-xs font-semibold whitespace-nowrap text-violet-300 rounded shrink-0" title={`Attempt ${task.attempt_count + 1}`}>
          ↺{task.attempt_count + 1}
        </span>
      )}

      {/* PR badge */}
      {task.pr_url && prNumberFromUrl(task.pr_url) != null && (
        <a
          href={task.pr_url}
          target="_blank"
          rel="noopener noreferrer"
          onClick={e => e.stopPropagation()}
          className="min-h-[44px] min-w-[44px] text-ui-xs text-sky-400/80 border border-sky-500/30 bg-sky-500/10 hover:bg-sky-500/20 hover:text-sky-300 px-1.5 rounded font-medium whitespace-nowrap flex items-center justify-center gap-0.5 shrink-0 transition-colors"
        >
          PR #{prNumberFromUrl(task.pr_url)}<ExternalLink className="w-2.5 h-2.5" />
        </a>
      )}

      {/* Iteration count badge */}
      {(() => {
        const prCount = task.artifacts?.filter(a => a.artifact_type === 'PULL_REQUEST').length ?? 0;
        return prCount > 1 ? (
          <span
            className="border border-violet-500/20 bg-violet-500/10 px-1.5 py-0.5 font-mono text-ui-xs whitespace-nowrap text-violet-300 rounded shrink-0"
            title={`This task required ${prCount} PR iterations`}
          >
            ×{prCount} iters
          </span>
        ) : null;
      })()}

      {/* Blocking badge */}
      {(blockingCount ?? 0) > 0 && (task.status === 'FAILED' || task.status === 'CANCELLED') && (
        <span className="inline-flex items-center gap-1 text-ui-xs font-medium bg-red-500/10 border border-red-500/30 text-red-400 px-1.5 py-0.5 rounded whitespace-nowrap shrink-0">
          <AlertTriangle className="w-2.5 h-2.5" />
          Blocking {blockingCount} visible
        </span>
      )}

      {/* Side-car presence/outcome badges — this row is the PRIMARY of one or
          more nested grader or finding-challenge tasks. */}
      {sidecarBadges?.map((b, i) => (
        <span
          key={`${b.kind}-${i}`}
          className={`text-ui-xs border px-1.5 py-0.5 rounded font-semibold whitespace-nowrap shrink-0 ${SIDECAR_TONE_CLASSES[b.tone]}`}
          title={b.title}
        >
          {b.text}
        </span>
      ))}

      {/* Title + status message */}
      <div className="flex flex-col min-w-0 flex-1">
        <span className="text-xs text-neutral-200 truncate group-hover:text-neutral-100">
          {task.title}
        </span>
        {task.latest_status_message && task.status !== 'COMPLETED' && task.status !== 'CANCELLED' && (
          <span className="text-ui-sm text-neutral-500 truncate">
            {task.latest_status_message}
          </span>
        )}
      </div>

      {/* Duration */}
      <span className="text-ui-sm text-neutral-500 font-mono shrink-0">
        {['QUEUED', 'PARKED', 'BLOCKED'].includes(task.status) ? 'Waiting' : duration}
      </span>
    </div>
  );
}

function TreeBranch({
  node,
  taskMap,
  allTaskIds,
  prChainMap,
  onViewLogs,
  onNavigateToParent,
  onUnarchiveParent,
  isLast,
  depth,
  blockingMap,
}: {
  node: TreeNode;
  taskMap: Map<string, Task>;
  allTaskIds: Set<string>;
  prChainMap: Map<number, Task>;
  onViewLogs: (task: Task) => void;
  onNavigateToParent?: (parentTaskId: string) => void;
  onUnarchiveParent?: (parentTaskId: string) => void;
  isLast: boolean;
  depth: number;
  blockingMap: Record<string, number>;
}) {
  const depIds = node.task.depends_on || [];
  const depTasks = depIds
    .map(id => taskMap.get(id))
    .filter((t): t is Task => !!t);
  const offPageDepIds = depIds.filter(id => !allTaskIds.has(id));

  // Find linked create task for review/fix tasks via PR number
  const linkedCreateTask = useMemo(() => {
    const tt = node.task.task_type;
    if (tt !== 'review' && tt !== 'fix') return null;
    const prNumber = (node.task.payloads?.[0] as Record<string, unknown> | undefined)?.pr_number;
    if (typeof prNumber !== 'number') return null;
    const createTask = prChainMap.get(prNumber);
    if (!createTask) return null;
    // Only show connector if the create task is not already a direct parent
    if (node.task.parent_task_id === createTask.id) return null;
    return createTask;
  }, [node.task, prChainMap]);

  const chainColor = node.task.task_type === 'fix' ? 'border-violet-500/50' : 'border-cyan-500/45';
  const chainBadgeBg = node.task.task_type === 'fix' ? 'border-violet-500/30 bg-violet-500/10 text-violet-300' : 'border-cyan-500/25 bg-cyan-500/10 text-cyan-300';
  const chainLabel = node.task.task_type === 'fix' ? 'Fix' : 'Review';

  const [childrenExpanded, setChildrenExpanded] = useState(true);
  const hasChildren = node.children.length > 0;

  // Side-cars nest under their primary, collapsed by default — they're
  // verification machinery, not pipeline work, so they stay out of the way
  // until the operator opens them.
  const [sidecarsExpanded, setSidecarsExpanded] = useState(false);
  const hasSidecars = node.sidecars.length > 0;
  const sidecarBadges = useMemo(
    () => node.sidecars
      .map(s => deriveSidecarBadge(s.task, node.task))
      .filter((b): b is SidecarBadge => b !== null),
    [node.sidecars, node.task],
  );

  return (
    <div className="relative">
      {/* Connector line from parent */}
      {depth > 0 && (
        <div className="absolute left-0 top-0 bottom-0 flex items-start" style={{ width: 20 }}>
          {/* Vertical line */}
          <div className={`absolute left-[9px] top-0 w-px bg-neutral-700 ${isLast ? 'h-[20px]' : 'h-full'}`} />
          {/* Horizontal line to node */}
          <div className="absolute left-[9px] top-[20px] h-px w-[11px] bg-neutral-700" />
        </div>
      )}

      <div style={{ paddingLeft: depth > 0 ? 20 : 0 }}>
        {/* depends_on connectors: dotted lines to each dependency target */}
        {(depTasks.length > 0 || offPageDepIds.length > 0) && (
          <div className="mb-1 ml-1 space-y-0.5">
            {depTasks.map(dep => {
              const depStyle = dep.task_type ? TASK_TYPE_STYLES[dep.task_type] : null;
              return (
                <div key={dep.id} className="flex items-center gap-1.5 min-w-0">
                  <div className="w-4 border-t-2 border-dashed border-neutral-600 shrink-0" />
                  <span className="text-ui-xs text-neutral-500 font-mono truncate max-w-[120px] sm:max-w-[200px]" title={`Depends on: ${dep.title}`}>
                    &#8594; {depStyle ? <span className={depStyle.text}>[{depStyle.label}]</span> : null} {dep.title}
                  </span>
                </div>
              );
            })}
            {offPageDepIds.map(id => (
              <div key={id} className="flex items-center gap-1.5 min-w-0">
                <div className="w-4 border-t-2 border-dashed border-amber-500/40 shrink-0" />
                <span className="text-ui-xs text-amber-500/70 font-mono truncate max-w-[120px] sm:max-w-[200px]" title={`Depends on task ${id} (not on this page)`}>
                  &#8594; depends on task not on this page ({id.slice(0, 8)})
                </span>
              </div>
            ))}
          </div>
        )}

        {/* PR chain connector: dotted arrow to originating create task */}
        {linkedCreateTask && (
          <div className="mb-1 ml-1 flex items-center gap-1.5 min-w-0">
            <div className={`w-4 border-t-2 border-dashed ${chainColor} shrink-0`} />
            <span className={`text-ui-xs font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded border ${chainBadgeBg} shrink-0`}>
              {chainLabel}
            </span>
            <span className="text-ui-xs text-neutral-500 font-mono truncate max-w-[100px] sm:max-w-[200px]" title={`${node.task.task_type === 'fix' ? 'Fixes' : 'Reviews'} PR from: ${linkedCreateTask.title}`}>
              ↳ {node.task.task_type === 'fix' ? 'Fixes' : 'Reviews'} PR from: {linkedCreateTask.title}
            </span>
          </div>
        )}

        <div className="flex items-center gap-1">
          {hasChildren && (
            <button
              onClick={e => { e.stopPropagation(); setChildrenExpanded(v => !v); }}
              className="shrink-0 min-h-[44px] min-w-[44px] inline-flex items-center justify-center text-neutral-500 hover:text-neutral-300 transition-colors"
              title={childrenExpanded ? 'Collapse children' : 'Expand children'}
            >
              {childrenExpanded
                ? <ChevronDown className="w-3.5 h-3.5" />
                : <ChevronRight className="w-3.5 h-3.5" />}
            </button>
          )}
          <div className="flex-1 min-w-0">
            <TaskNode task={node.task} onViewLogs={onViewLogs} isOrphan={node.isOrphan} onNavigateToParent={onNavigateToParent} onUnarchiveParent={onUnarchiveParent} blockingCount={blockingMap[node.task.id]} sidecarBadges={sidecarBadges.length > 0 ? sidecarBadges : undefined} isSidecar={node.isSidecar} isSidecarFallback={node.isSidecarFallback} />
          </div>
        </div>

        {/* Side-cars: graders/challengers back-referencing this task, nested
            and collapsed by default. */}
        {hasSidecars && (
          <>
            <div style={{ paddingLeft: 20 }}>
              <button
                onClick={e => { e.stopPropagation(); setSidecarsExpanded(v => !v); }}
                className="min-h-[44px] inline-flex items-center gap-1 text-ui-sm text-violet-400/80 hover:text-violet-300 transition-colors"
                title={sidecarsExpanded ? 'Collapse side-cars' : 'Expand side-cars'}
              >
                {sidecarsExpanded
                  ? <ChevronDown className="w-3 h-3 shrink-0" />
                  : <ChevronRight className="w-3 h-3 shrink-0" />}
                {node.sidecars.length} side-car{node.sidecars.length === 1 ? '' : 's'}
              </button>
            </div>
            {sidecarsExpanded && (
              <div className="mt-1 space-y-1">
                {node.sidecars.map((child, i) => (
                  <TreeBranch
                    key={child.task.id}
                    node={child}
                    taskMap={taskMap}
                    allTaskIds={allTaskIds}
                    prChainMap={prChainMap}
                    onViewLogs={onViewLogs}
                    onNavigateToParent={onNavigateToParent}
                    onUnarchiveParent={onUnarchiveParent}
                    isLast={i === node.sidecars.length - 1}
                    depth={depth + 1}
                    blockingMap={blockingMap}
                  />
                ))}
              </div>
            )}
          </>
        )}

        {/* Children */}
        {hasChildren && childrenExpanded && (
          <div className="mt-1 space-y-1">
            {node.children.map((child, i) => (
              <TreeBranch
                key={child.task.id}
                node={child}
                taskMap={taskMap}
                allTaskIds={allTaskIds}
                prChainMap={prChainMap}
                onViewLogs={onViewLogs}
                onNavigateToParent={onNavigateToParent}
                onUnarchiveParent={onUnarchiveParent}
                isLast={i === node.children.length - 1}
                depth={depth + 1}
                blockingMap={blockingMap}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/** Extract PR number from a pr_url like https://github.com/owner/repo/pull/123 */
function prNumberFromUrl(url: string): number | null {
  const m = url.match(/\/pull\/(\d+)/);
  return m ? Number(m[1]) : null;
}

/** Build a map from PR number → create task that produced the PR */
function buildPrChainMap(tasks: Task[]): Map<number, Task> {
  const map = new Map<number, Task>();
  for (const t of tasks) {
    if (t.task_type === 'create' && t.pr_url) {
      const num = prNumberFromUrl(t.pr_url);
      if (num != null) map.set(num, t);
    }
  }
  return map;
}

function collectAllIds(node: TreeNode): string[] {
  return [
    node.task.id,
    ...node.children.flatMap(c => collectAllIds(c)),
    ...node.sidecars.flatMap(c => collectAllIds(c)),
  ];
}

function RootGroup({
  root,
  taskMap,
  allTaskIds,
  prChainMap,
  onViewLogs,
  onNavigateToParent,
  onUnarchiveParent,
  expanded,
  onToggle,
  onArchiveGroup,
  blockingMap,
}: {
  root: TreeNode;
  taskMap: Map<string, Task>;
  allTaskIds: Set<string>;
  prChainMap: Map<number, Task>;
  onViewLogs: (task: Task) => void;
  onNavigateToParent?: (parentTaskId: string) => void;
  onUnarchiveParent?: (parentTaskId: string) => void;
  expanded: boolean;
  onToggle: () => void;
  onArchiveGroup?: (taskIds: string[]) => void;
  blockingMap: Record<string, number>;
}) {
  const isSynthetic = root.isSyntheticParent;

  // Single tree traversal returns all status counts at once instead of
  // 10 separate walks (finding 4: 10x traversal → 1x traversal).
  const statusCounts: StatusCounts = isSynthetic
    ? root.children.reduce<StatusCounts>(
        (acc, c) => {
          const childCounts = countAllStatuses(c);
          return {
            total: acc.total + childCounts.total,
            COMPLETED: acc.COMPLETED + childCounts.COMPLETED,
            IN_PROGRESS: acc.IN_PROGRESS + childCounts.IN_PROGRESS,
            PROVISIONING: acc.PROVISIONING + childCounts.PROVISIONING,
            EVALUATING: acc.EVALUATING + childCounts.EVALUATING,
            FAILED: acc.FAILED + childCounts.FAILED,
            CANCELLED: acc.CANCELLED + childCounts.CANCELLED,
            BLOCKED: acc.BLOCKED + childCounts.BLOCKED,
            PARKED: acc.PARKED + childCounts.PARKED,
            QUEUED: acc.QUEUED + childCounts.QUEUED,
          };
        },
        { total: 0, COMPLETED: 0, IN_PROGRESS: 0, PROVISIONING: 0, EVALUATING: 0, FAILED: 0, CANCELLED: 0, BLOCKED: 0, PARKED: 0, QUEUED: 0 }
      )
    : countAllStatuses(root);

  const {
    total: totalInGroup,
    COMPLETED: completedInGroup,
    IN_PROGRESS: inProgressInGroup,
    PROVISIONING: provisioningInGroup,
    EVALUATING: evaluatingInGroup,
    FAILED: failedInGroup,
    CANCELLED: cancelledInGroup,
    BLOCKED: blockedInGroup,
    PARKED: parkedInGroup,
    QUEUED: queuedInGroup,
  } = statusCounts;
  const runningInGroup = inProgressInGroup + provisioningInGroup + evaluatingInGroup;

  return (
    <div
      className={isSynthetic
        ? 'rounded-md border border-dashed border-neutral-700/60 bg-[#10151c] p-3'
        : 'rounded-md border border-neutral-800 bg-[#10151c] p-3'}
    >
      {/* Group header — double-click to archive */}
      <div className="group flex w-full items-center justify-between px-0.5">
        <div className="flex items-center gap-2 truncate pr-4">
          <div
            role="button"
            tabIndex={0}
            onClick={onToggle}
            onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onToggle(); } }}
            className="flex items-center gap-2 truncate cursor-pointer"
          >
            <ChevronDown
              className={`w-3.5 h-3.5 shrink-0 text-neutral-500 transition-transform duration-150 ${expanded ? '' : '-rotate-90'}`}
            />
            <h3 className={`text-ui-md font-bold uppercase tracking-widest truncate ${isSynthetic ? 'text-neutral-500' : 'text-neutral-400'}`}>
              {root.task.title}
            </h3>
          </div>
          {isSynthetic && root.task.parent_task_id && (() => {
            const parentId = root.task.parent_task_id;
            return (
            <button
              onClick={() => onNavigateToParent?.(parentId)}
              className="min-h-[44px] min-w-[44px] inline-flex items-center justify-center text-ui-xs text-amber-500/70 border border-amber-500/30 bg-amber-500/10 hover:bg-amber-500/20 hover:text-amber-400 px-1.5 rounded font-medium whitespace-nowrap shrink-0 transition-colors cursor-pointer"
              title={`Copy parent task ID: ${root.task.parent_task_id}`}
            >
              ↑ Parent on another page
            </button>
            );
          })()}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {onArchiveGroup && runningInGroup === 0 && !isSynthetic && (
            <button
              onClick={e => { e.stopPropagation(); onArchiveGroup(collectAllIds(root).filter(id => !id.startsWith('synthetic-'))); }}
              className="opacity-100 sm:opacity-0 sm:group-hover:opacity-100 transition-opacity text-ui-sm text-neutral-500 hover:text-neutral-300 min-h-[44px] min-w-[44px] flex items-center justify-center gap-1 px-2 rounded border border-neutral-700 hover:border-neutral-600 mr-1"
              title="Archive this group"
            >
              <Archive className="w-3 h-3" />
            </button>
          )}
          {runningInGroup > 0 && (
            <span className="flex items-center gap-1 rounded border border-yellow-500/30 bg-yellow-500/10 px-1.5 py-0.5 text-ui-sm font-medium text-yellow-400">
              <span className="w-1.5 h-1.5 rounded-full bg-yellow-400 shrink-0" />
              {runningInGroup}
            </span>
          )}
          {blockedInGroup > 0 && (
            <span className="flex items-center gap-1 rounded border border-amber-500/30 bg-amber-500/10 px-1.5 py-0.5 text-ui-sm font-medium text-amber-400" title={`${blockedInGroup} blocked`}>
              <span className="w-1.5 h-1.5 rounded-full bg-amber-400 shrink-0" />
              {blockedInGroup}
            </span>
          )}
          {parkedInGroup > 0 && (
            <span className="flex items-center gap-1 rounded border border-purple-500/30 bg-purple-500/10 px-1.5 py-0.5 text-ui-sm font-medium text-purple-400" title={`${parkedInGroup} parked`}>
              <span className="w-1.5 h-1.5 rounded-full bg-purple-400 shrink-0" />
              {parkedInGroup}
            </span>
          )}
          {failedInGroup > 0 && (
            <span className="flex items-center gap-1 rounded border border-red-500/30 bg-red-500/10 px-1.5 py-0.5 text-ui-sm font-medium text-red-400">
              <span className="w-1.5 h-1.5 rounded-full bg-red-400 shrink-0" />
              {failedInGroup}
            </span>
          )}
          <span className="rounded border border-neutral-700 bg-neutral-900 px-1.5 py-0.5 text-ui-sm font-medium text-neutral-300">
            {completedInGroup}/{totalInGroup - cancelledInGroup} done
          </span>
        </div>
      </div>

      {/* Progress bar */}
      {(() => {
        const segments = [
          { count: queuedInGroup, color: taskStatusDotColor('QUEUED') },
          { count: parkedInGroup, color: taskStatusDotColor('PARKED') },
          { count: blockedInGroup, color: taskStatusDotColor('BLOCKED') },
          { count: provisioningInGroup, color: taskStatusDotColor('PROVISIONING') },
          { count: inProgressInGroup, color: taskStatusDotColor('IN_PROGRESS') },
          { count: evaluatingInGroup, color: taskStatusDotColor('EVALUATING') },
          { count: failedInGroup, color: taskStatusDotColor('FAILED') },
          { count: completedInGroup, color: taskStatusDotColor('COMPLETED') },
        ].filter(s => s.count > 0);
        return (
          <div className="mt-2 flex h-1 w-full overflow-hidden rounded-full bg-neutral-900">
            {segments.map((s, i) => (
              <div
                key={i}
                className={s.color}
                style={{ flexGrow: s.count }}
              />
            ))}
          </div>
        );
      })()}

      {/* Tree */}
      {expanded && (
        <div className="mt-2.5 space-y-1">
          {isSynthetic ? (
            root.children.map((child, i) => (
              <TreeBranch
                key={child.task.id}
                node={child}
                taskMap={taskMap}
                allTaskIds={allTaskIds}
                prChainMap={prChainMap}
                onViewLogs={onViewLogs}
                onNavigateToParent={onNavigateToParent}
                onUnarchiveParent={onUnarchiveParent}
                isLast={i === root.children.length - 1}
                depth={0}
                blockingMap={blockingMap}
              />
            ))
          ) : (
            <TreeBranch
              node={root}
              taskMap={taskMap}
              allTaskIds={allTaskIds}
              prChainMap={prChainMap}
              onViewLogs={onViewLogs}
              onNavigateToParent={onNavigateToParent}
              onUnarchiveParent={onUnarchiveParent}
              isLast={true}
              depth={0}
              blockingMap={blockingMap}
            />
          )}
        </div>
      )}
    </div>
  );
}

export function PipelineView({ tasks, loading, onViewLogs, hasActiveFilters, onNavigateToParent, onClearWaveDeps, onArchiveGroup, onUnarchiveTask }: PipelineViewProps) {
  const forest = useMemo(() => buildForest(tasks), [tasks]);
  const allTaskIds = useMemo(() => new Set(tasks.map(t => t.id)), [tasks]);
  const taskMap = useMemo(() => new Map(tasks.map(t => [t.id, t])), [tasks]);
  const prChainMap = useMemo(() => buildPrChainMap(tasks), [tasks]);
  const blockingMap = useMemo(() => {
    const map: Record<string, number> = {};
    for (const task of tasks) {
      if (task.depends_on) {
        for (const depId of task.depends_on) {
          map[depId] = (map[depId] ?? 0) + 1;
        }
      }
    }
    return map;
  }, [tasks]);
  const [groupExpanded, setGroupExpanded] = useState<Record<string, boolean>>({});

  // Hide synthetic orphan groups whose parent was archived and every child is
  // now terminal — they have no actionable tasks and only add clutter.
  const visibleForest = useMemo(() =>
    forest.filter(root => {
      if (!root.isSyntheticParent) return true;
      const childIds = collectAllIds(root).filter(id => !id.startsWith('synthetic-'));
      if (childIds.length === 0) return false;
      return !childIds.every(id => {
        const t = taskMap.get(id);
        return t && TERMINAL_STATUSES.includes(t.status);
      });
    }),
    [forest, taskMap],
  );

  const defaultExpanded = useMemo(() =>
    Object.fromEntries(
      visibleForest.map(r => [
        r.task.id,
        countByStatuses(r, ['IN_PROGRESS', 'PROVISIONING', 'EVALUATING', 'FAILED', 'BLOCKED', 'PARKED']) > 0
          || countQueuedBehindDependencies(r) > 0,
      ])
    ),
    [visibleForest]
  );

  const getExpanded = (id: string) => id in groupExpanded ? groupExpanded[id] : (defaultExpanded[id] ?? true);
  const toggleGroup = (id: string) => setGroupExpanded(prev => ({ ...prev, [id]: !getExpanded(id) }));
  const expandAll = () => setGroupExpanded(Object.fromEntries(visibleForest.map(r => [r.task.id, true])));
  const collapseAll = () => setGroupExpanded(Object.fromEntries(visibleForest.map(r => [r.task.id, false])));

  const activeTaskIds = useMemo(
    () => tasks.filter(t => ACTIVE_STATUSES.includes(t.status)).map(t => t.id),
    [tasks],
  );

  const jumpToActive = () => {
    // Expand all root groups that contain active tasks
    const toExpand: Record<string, boolean> = {};
    for (const root of visibleForest) {
      const hasActive = root.isSyntheticParent
        ? root.children.some(c => countByStatuses(c, ACTIVE_STATUSES) > 0)
        : countByStatuses(root, ACTIVE_STATUSES) > 0;
      if (hasActive) toExpand[root.task.id] = true;
    }
    setGroupExpanded(prev => ({ ...prev, ...toExpand }));

    // After re-render, scroll to the first active task element
    setTimeout(() => {
      const firstActiveId = activeTaskIds[0];
      if (!firstActiveId) return;
      const el = document.querySelector(`[data-task-id="${firstActiveId}"]`);
      el?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }, 0);
  };

  // Pipelines where all visible (non-synthetic) nodes are terminal (done).
  // Synthetic parents are orphan-groups whose real parent is archived or on
  // another page — if every visible child in the group is terminal, the
  // tree is done from the operator's view and should be archivable too.
  const donePipelineIds = useMemo(() => {
    const ids: string[] = [];
    for (const root of visibleForest) {
      const allIds = collectAllIds(root).filter(id => !id.startsWith('synthetic-'));
      if (allIds.length === 0) continue;
      const allTerminal = allIds.every(id => {
        const t = taskMap.get(id);
        return t && TERMINAL_STATUSES.includes(t.status);
      });
      if (allTerminal) ids.push(...allIds);
    }
    return ids;
  }, [visibleForest, taskMap]);

  const btnClass = 'px-2 py-1 text-xs text-neutral-500 hover:text-neutral-300 bg-neutral-900 border border-neutral-800 rounded transition-colors';

  return (
    <div className="flex-1 overflow-y-auto overflow-x-auto pb-4 custom-scrollbar">
      <div className="space-y-4">
        {onClearWaveDeps && tasks.some(t => t.status === 'QUEUED' && t.depends_on && t.depends_on.length > 0) && (
          <div className="flex items-center gap-1.5">
            <button
              className="px-2.5 py-1 text-xs rounded-full border border-red-500/30 bg-red-500/10 text-red-400 hover:bg-red-500/20 transition-colors font-medium"
              onClick={() => {
                if (window.confirm('Clear stale/legacy wave serialization edges from queued tasks? True prerequisites (research prerequisites, blocked-need gates, sibling foundations) are preserved.')) {
                  onClearWaveDeps();
                }
              }}
            >
              Clear Stale Wave Deps
            </button>
          </div>
        )}
        {visibleForest.length > 1 && (
          <div className="flex items-center gap-2">
            <button className={btnClass} onClick={expandAll}>Expand all</button>
            <button className={btnClass} onClick={collapseAll}>Collapse all</button>
            {activeTaskIds.length > 0 && (
              <button
                className="min-h-[44px] min-w-[44px] inline-flex items-center justify-center px-2 text-xs text-yellow-400 hover:text-yellow-300 bg-yellow-500/10 border border-yellow-500/30 rounded transition-colors"
                onClick={jumpToActive}
              >
                Jump to active
              </button>
            )}
            {onArchiveGroup && donePipelineIds.length > 0 && (
              <button
                className="min-h-[44px] min-w-[44px] flex items-center justify-center gap-1 px-2 text-xs text-neutral-500 hover:text-neutral-300 bg-neutral-900 border border-neutral-800 rounded transition-colors"
                onClick={() => onArchiveGroup(donePipelineIds)}
              >
                <Archive className="w-3 h-3" />
                Archive done ({donePipelineIds.length})
              </button>
            )}
          </div>
        )}
        {visibleForest.length === 0 && (
          <div className="text-center text-neutral-600 text-xs py-16 border border-dashed border-neutral-800 rounded-xl bg-neutral-900/20">
            {loading
              ? 'Loading tasks...'
              : hasActiveFilters
                ? 'No tasks match the active filters — try clearing filters to see all tasks'
                : 'No tasks yet'}
          </div>
        )}
        {visibleForest.map(root => (
          <RootGroup
            key={root.task.id}
            root={root}
            taskMap={taskMap}
            allTaskIds={allTaskIds}
            prChainMap={prChainMap}
            onViewLogs={onViewLogs}
            onNavigateToParent={onNavigateToParent}
            onUnarchiveParent={onUnarchiveTask}
            expanded={getExpanded(root.task.id)}
            onToggle={() => toggleGroup(root.task.id)}
            onArchiveGroup={onArchiveGroup}
            blockingMap={blockingMap}
          />
        ))}
      </div>
    </div>
  );
}

function countByStatuses(node: TreeNode, statuses: string[]): number {
  const self = statuses.includes(node.task.status) ? 1 : 0;
  return self
    + node.children.reduce((sum, c) => sum + countByStatuses(c, statuses), 0)
    + node.sidecars.reduce((sum, c) => sum + countByStatuses(c, statuses), 0);
}

type StatusCounts = {
  total: number;
  COMPLETED: number;
  IN_PROGRESS: number;
  PROVISIONING: number;
  EVALUATING: number;
  FAILED: number;
  CANCELLED: number;
  BLOCKED: number;
  PARKED: number;
  QUEUED: number;
};

function countAllStatuses(root: TreeNode): StatusCounts {
  const counts: StatusCounts = {
    total: 0, COMPLETED: 0, IN_PROGRESS: 0, PROVISIONING: 0,
    EVALUATING: 0, FAILED: 0, CANCELLED: 0, BLOCKED: 0,
    PARKED: 0, QUEUED: 0,
  };
  function walk(node: TreeNode) {
    counts.total++;
    const status = node.task.status as keyof StatusCounts;
    if (status in counts) { counts[status]++; }
    for (const child of node.children) walk(child);
    for (const sidecar of node.sidecars) walk(sidecar);
  }
  walk(root);
  return counts;
}

function countQueuedBehindDependencies(node: TreeNode): number {
  const self = node.task.status === 'QUEUED' && (node.task.depends_on?.length ?? 0) > 0 ? 1 : 0;
  return self
    + node.children.reduce((sum, c) => sum + countQueuedBehindDependencies(c), 0)
    + node.sidecars.reduce((sum, c) => sum + countQueuedBehindDependencies(c), 0);
}
