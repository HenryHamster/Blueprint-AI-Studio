import { useMemo, useState } from 'react';
import { ChevronDown, ChevronRight, ExternalLink } from 'lucide-react';
import type { Task } from './TaskCard';
import { TASK_TYPE_STYLES, useElapsedTime, getHeartbeatStatus } from './TaskCard';

const ACTIVE_STATUSES = ['IN_PROGRESS', 'PROVISIONING', 'EVALUATING'];

const STATUS_COLORS: Record<string, string> = {
  COMPLETED: 'bg-emerald-500',
  IN_PROGRESS: 'bg-yellow-500',
  PROVISIONING: 'bg-yellow-500',
  EVALUATING: 'bg-yellow-500',
  FAILED: 'bg-red-500',
  CANCELLED: 'bg-red-500',
  QUEUED: 'bg-neutral-500',
};

interface TreeNode {
  task: Task;
  children: TreeNode[];
  isSyntheticParent?: boolean;
  isOrphan?: boolean;
}

type FilterType = 'All' | 'Active' | 'Failed' | 'Needs Review';

const FILTER_CHIPS: { label: FilterType }[] = [
  { label: 'All' },
  { label: 'Active' },
  { label: 'Failed' },
  { label: 'Needs Review' },
];

function taskMatchesFilter(task: Task, filter: FilterType): boolean {
  if (filter === 'All') return true;
  if (filter === 'Active') return ['IN_PROGRESS', 'PROVISIONING', 'EVALUATING'].includes(task.status);
  if (filter === 'Failed') return task.status === 'FAILED';
  if (filter === 'Needs Review') return task.requires_approval === true;
  return true;
}

interface PipelineViewProps {
  tasks: Task[];
  onViewLogs: (task: Task) => void;
  onNavigateToParent?: (parentTaskId: string) => void;
}

function buildForest(tasks: Task[]): TreeNode[] {
  const taskMap = new Map<string, Task>();
  for (const t of tasks) taskMap.set(t.id, t);

  const nodeMap = new Map<string, TreeNode>();
  for (const t of tasks) nodeMap.set(t.id, { task: t, children: [] });

  const roots: TreeNode[] = [];
  // Group orphans (tasks whose parent is not on this page) by parent_task_id
  const orphanGroups = new Map<string, TreeNode[]>();

  for (const t of tasks) {
    const node = nodeMap.get(t.id)!;
    if (t.parent_task_id && nodeMap.has(t.parent_task_id)) {
      nodeMap.get(t.parent_task_id)!.children.push(node);
    } else if (t.parent_task_id && !nodeMap.has(t.parent_task_id)) {
      // Orphan: parent exists but is not on this page
      node.isOrphan = true;
      const group = orphanGroups.get(t.parent_task_id) || [];
      group.push(node);
      orphanGroups.set(t.parent_task_id, group);
    } else {
      roots.push(node);
    }
  }

  // Create synthetic parent nodes for orphan groups
  const syntheticRoots: TreeNode[] = [];
  for (const [parentId, orphans] of orphanGroups) {
    const shortId = parentId.slice(0, 8);
    const syntheticParent: TreeNode = {
      task: {
        id: `synthetic-${parentId}`,
        title: `Task group (parent: ${shortId})`,
        description: '',
        status: 'QUEUED',
        created_by: '',
        requires_approval: false,
        parent_task_id: parentId,
        created_at: orphans[0].task.created_at,
        history: [],
      } as Task,
      children: orphans,
      isSyntheticParent: true,
    };
    syntheticRoots.push(syntheticParent);
  }

  // Sort children by pipeline order: research → plan → create → review → fix
  const typeOrder: Record<string, number> = { research: 0, plan: 1, create: 2, review: 3, fix: 4 };
  const sortChildren = (node: TreeNode) => {
    node.children.sort((a, b) => (typeOrder[a.task.task_type || 'create'] ?? 2) - (typeOrder[b.task.task_type || 'create'] ?? 2));
    node.children.forEach(sortChildren);
  };
  roots.forEach(sortChildren);
  syntheticRoots.forEach(sortChildren);

  // Sort roots by creation time (newest first)
  roots.sort((a, b) => new Date(b.task.created_at).getTime() - new Date(a.task.created_at).getTime());
  syntheticRoots.sort((a, b) => new Date(b.task.created_at).getTime() - new Date(a.task.created_at).getTime());

  // Synthetic groups appear first so users see incomplete groups at the top
  return [...syntheticRoots, ...roots];
}

function TaskNode({ task, onViewLogs, isOrphan, onNavigateToParent, activeFilter }: { task: Task; onViewLogs: (task: Task) => void; isOrphan?: boolean; onNavigateToParent?: (parentTaskId: string) => void; activeFilter: FilterType }) {
  const style = task.task_type ? TASK_TYPE_STYLES[task.task_type] : null;
  const Icon = style?.icon;
  const statusColor = STATUS_COLORS[task.status] || 'bg-neutral-500';
  const isRunning = task.status === 'IN_PROGRESS' || task.status === 'PROVISIONING';
  const heartbeat = isRunning && task.last_heartbeat ? getHeartbeatStatus(task.last_heartbeat) : null;

  const isActiveForDuration = task.status === 'IN_PROGRESS' || task.status === 'PROVISIONING';
  const isTerminal = task.status === 'COMPLETED' || task.status === 'FAILED' || task.status === 'CANCELLED';
  const endTime = isTerminal && task.history && task.history.length > 0
    ? task.history[task.history.length - 1].timestamp
    : isTerminal ? task.created_at : undefined;
  const duration = useElapsedTime(task.created_at, isActiveForDuration ? undefined : endTime);
  const dimmed = !taskMatchesFilter(task, activeFilter);

  return (
    <div
      role="button"
      tabIndex={0}
      data-task-id={task.id}
      onClick={() => onViewLogs(task)}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onViewLogs(task); } }}
      className={`flex items-center gap-2.5 bg-neutral-900 border rounded-lg px-3 py-2 hover:border-neutral-600 hover:bg-neutral-800/80 transition-all cursor-pointer text-left w-full min-w-0 group ${isOrphan ? 'border-dashed border-neutral-600' : 'border-neutral-800'} ${dimmed ? 'opacity-40 grayscale' : ''}`}
      title={`${task.title}\n${task.status} • ${task.task_type || 'create'}\nClick to view logs`}
    >
      {/* Status dot */}
      <div className={`w-2.5 h-2.5 rounded-full shrink-0 ${statusColor}`} />
      {heartbeat && (
        <div className={`w-1.5 h-1.5 rounded-full shrink-0 ${heartbeat.color}${heartbeat.color === 'bg-red-500' ? ' animate-pulse' : ''}`} title={heartbeat.text} />
      )}

      {/* Orphan indicator */}
      {isOrphan && task.parent_task_id && (
        <button
          onClick={e => { e.stopPropagation(); onNavigateToParent?.(task.parent_task_id!); }}
          className="text-[9px] text-amber-500/80 border border-amber-500/30 bg-amber-500/10 hover:bg-amber-500/20 hover:text-amber-400 px-1.5 py-0.5 rounded font-medium whitespace-nowrap shrink-0 transition-colors cursor-pointer"
          title={`Copy parent task ID: ${task.parent_task_id}`}
        >
          ↑ Parent on another page
        </button>
      )}

      {/* Type badge */}
      {style && Icon && (
        <span className={`${style.bg} ${style.text} border text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded font-semibold whitespace-nowrap flex items-center gap-0.5 shrink-0`}>
          <Icon className="w-2.5 h-2.5" />{style.label}
        </span>
      )}

      {/* Retry badge */}
      {task.attempt_count != null && task.attempt_count > 0 && (
        <span className="text-orange-400 bg-orange-500/10 border border-orange-500/20 text-[9px] px-1.5 py-0.5 rounded font-semibold whitespace-nowrap shrink-0" title={`Attempt ${task.attempt_count + 1}`}>
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
          className="text-[9px] text-sky-400/80 border border-sky-500/30 bg-sky-500/10 hover:bg-sky-500/20 hover:text-sky-300 px-1.5 py-0.5 rounded font-medium whitespace-nowrap flex items-center gap-0.5 shrink-0 transition-colors"
        >
          PR #{prNumberFromUrl(task.pr_url)}<ExternalLink className="w-2.5 h-2.5" />
        </a>
      )}

      {/* Iteration count badge */}
      {(() => {
        const prCount = task.artifacts?.filter(a => a.artifact_type === 'PULL_REQUEST').length ?? 0;
        return prCount > 1 ? (
          <span
            className="bg-orange-500/10 border border-orange-500/20 text-orange-400 text-[9px] font-mono px-1.5 py-0.5 rounded whitespace-nowrap shrink-0"
            title={`This task required ${prCount} PR iterations`}
          >
            ×{prCount} iters
          </span>
        ) : null;
      })()}

      {/* Title + status message */}
      <div className="flex flex-col min-w-0 flex-1">
        <span className="text-xs text-neutral-200 truncate group-hover:text-neutral-100">
          {task.title}
        </span>
        {task.latest_status_message && task.status !== 'QUEUED' && task.status !== 'COMPLETED' && task.status !== 'CANCELLED' && (
          <span className="text-[10px] text-neutral-500 truncate">
            {task.latest_status_message}
          </span>
        )}
      </div>

      {/* Duration */}
      <span className="text-[10px] text-neutral-500 font-mono shrink-0">
        {task.status === 'QUEUED' ? 'Waiting' : duration}
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
  isLast,
  depth,
  activeFilter,
}: {
  node: TreeNode;
  taskMap: Map<string, Task>;
  allTaskIds: Set<string>;
  prChainMap: Map<number, Task>;
  onViewLogs: (task: Task) => void;
  onNavigateToParent?: (parentTaskId: string) => void;
  isLast: boolean;
  depth: number;
  activeFilter: FilterType;
}) {
  const depIds = node.task.depends_on || [];
  const depTasks = depIds
    .filter(id => allTaskIds.has(id))
    .map(id => taskMap.get(id)!)
    .filter(Boolean);
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

  const chainColor = node.task.task_type === 'fix' ? 'border-orange-500/50' : 'border-amber-500/50';
  const chainBadgeBg = node.task.task_type === 'fix' ? 'bg-orange-500/10 border-orange-500/30 text-orange-400' : 'bg-amber-500/10 border-amber-500/30 text-amber-400';
  const chainLabel = node.task.task_type === 'fix' ? 'Fix' : 'Review';

  const [childrenExpanded, setChildrenExpanded] = useState(true);
  const hasChildren = node.children.length > 0;

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
                <div key={dep.id} className="flex items-center gap-1.5">
                  <div className="w-4 border-t-2 border-dashed border-neutral-600" />
                  <span className="text-[9px] text-neutral-500 font-mono truncate max-w-[200px]" title={`Depends on: ${dep.title}`}>
                    &#8594; {depStyle ? <span className={depStyle.text}>[{depStyle.label}]</span> : null} {dep.title}
                  </span>
                </div>
              );
            })}
            {offPageDepIds.map(id => (
              <div key={id} className="flex items-center gap-1.5">
                <div className="w-4 border-t-2 border-dashed border-amber-500/40" />
                <span className="text-[9px] text-amber-500/70 font-mono truncate max-w-[200px]" title={`Depends on task ${id} (not on this page)`}>
                  &#8594; depends on task not on this page ({id.slice(0, 8)})
                </span>
              </div>
            ))}
          </div>
        )}

        {/* PR chain connector: dotted arrow to originating create task */}
        {linkedCreateTask && (
          <div className="mb-1 ml-1 flex items-center gap-1.5">
            <div className={`w-4 border-t-2 border-dashed ${chainColor}`} />
            <span className={`text-[9px] font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded border ${chainBadgeBg}`}>
              {chainLabel}
            </span>
            <span className="text-[9px] text-neutral-500 font-mono truncate max-w-[200px]" title={`${node.task.task_type === 'fix' ? 'Fixes' : 'Reviews'} PR from: ${linkedCreateTask.title}`}>
              ↳ {node.task.task_type === 'fix' ? 'Fixes' : 'Reviews'} PR from: {linkedCreateTask.title}
            </span>
          </div>
        )}

        <div className="flex items-center gap-1">
          {hasChildren && (
            <button
              onClick={e => { e.stopPropagation(); setChildrenExpanded(v => !v); }}
              className="shrink-0 p-0.5 text-neutral-500 hover:text-neutral-300 transition-colors"
              title={childrenExpanded ? 'Collapse children' : 'Expand children'}
            >
              {childrenExpanded
                ? <ChevronDown className="w-3.5 h-3.5" />
                : <ChevronRight className="w-3.5 h-3.5" />}
            </button>
          )}
          <div className="flex-1 min-w-0">
            <TaskNode task={node.task} onViewLogs={onViewLogs} isOrphan={node.isOrphan} onNavigateToParent={onNavigateToParent} activeFilter={activeFilter} />
          </div>
        </div>

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
                isLast={i === node.children.length - 1}
                depth={depth + 1}
                activeFilter={activeFilter}
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

function RootGroup({
  root,
  taskMap,
  allTaskIds,
  prChainMap,
  onViewLogs,
  onNavigateToParent,
  expanded,
  onToggle,
  activeFilter,
}: {
  root: TreeNode;
  taskMap: Map<string, Task>;
  allTaskIds: Set<string>;
  prChainMap: Map<number, Task>;
  onViewLogs: (task: Task) => void;
  onNavigateToParent?: (parentTaskId: string) => void;
  expanded: boolean;
  onToggle: () => void;
  activeFilter: FilterType;
}) {
  const isSynthetic = root.isSyntheticParent;
  const totalInGroup = isSynthetic
    ? root.children.reduce((sum, c) => sum + countNodes(c), 0)
    : countNodes(root);
  const completedInGroup = isSynthetic
    ? root.children.reduce((sum, c) => sum + countByStatus(c, 'COMPLETED'), 0)
    : countByStatus(root, 'COMPLETED');
  const runningInGroup = isSynthetic
    ? root.children.reduce((sum, c) => sum + countByStatuses(c, ['IN_PROGRESS', 'PROVISIONING', 'EVALUATING']), 0)
    : countByStatuses(root, ['IN_PROGRESS', 'PROVISIONING', 'EVALUATING']);
  const failedInGroup = isSynthetic
    ? root.children.reduce((sum, c) => sum + countByStatuses(c, ['FAILED']), 0)
    : countByStatuses(root, ['FAILED']);

  return (
    <div
      className={isSynthetic
        ? 'bg-neutral-900/30 border border-dashed border-neutral-700/60 rounded-xl p-4'
        : 'bg-neutral-900/50 border border-neutral-800/50 rounded-xl p-4'}
    >
      {/* Group header */}
      <div className="flex items-center justify-between w-full mb-0 px-1 group">
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
            <h3 className={`text-[11px] font-bold uppercase tracking-widest truncate ${isSynthetic ? 'text-neutral-500' : 'text-neutral-400'}`}>
              {root.task.title}
            </h3>
          </div>
          {isSynthetic && root.task.parent_task_id && (
            <button
              onClick={() => onNavigateToParent?.(root.task.parent_task_id!)}
              className="text-[9px] text-amber-500/70 border border-amber-500/30 bg-amber-500/10 hover:bg-amber-500/20 hover:text-amber-400 px-1.5 py-0.5 rounded font-medium whitespace-nowrap shrink-0 transition-colors cursor-pointer"
              title={`Copy parent task ID: ${root.task.parent_task_id}`}
            >
              ↑ Parent on another page
            </button>
          )}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {runningInGroup > 0 && (
            <span className="flex items-center gap-1 text-[10px] bg-yellow-500/10 text-yellow-400 px-2 py-0.5 rounded-full font-medium border border-yellow-500/30">
              <span className="w-1.5 h-1.5 rounded-full bg-yellow-400 shrink-0" />
              {runningInGroup}
            </span>
          )}
          {failedInGroup > 0 && (
            <span className="flex items-center gap-1 text-[10px] bg-red-500/10 text-red-400 px-2 py-0.5 rounded-full font-medium border border-red-500/30">
              <span className="w-1.5 h-1.5 rounded-full bg-red-400 shrink-0" />
              {failedInGroup}
            </span>
          )}
          <span className="text-[10px] bg-neutral-800 text-neutral-300 px-2 py-0.5 rounded-full font-medium border border-neutral-700">
            {completedInGroup}/{totalInGroup} done
          </span>
        </div>
      </div>

      {/* Progress bar */}
      {(() => {
        const queuedInGroup = totalInGroup - completedInGroup - runningInGroup - failedInGroup;
        const segments = [
          { count: queuedInGroup, color: 'bg-neutral-700' },
          { count: runningInGroup, color: 'bg-yellow-500' },
          { count: failedInGroup, color: 'bg-red-500' },
          { count: completedInGroup, color: 'bg-emerald-500' },
        ].filter(s => s.count > 0);
        return (
          <div className="flex w-full h-1 rounded-full overflow-hidden mt-2">
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
        <div className="space-y-1 mt-3">
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
                isLast={i === root.children.length - 1}
                depth={0}
                activeFilter={activeFilter}
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
              isLast={true}
              depth={0}
              activeFilter={activeFilter}
            />
          )}
        </div>
      )}
    </div>
  );
}

export function PipelineView({ tasks, onViewLogs, onNavigateToParent }: PipelineViewProps) {
  const forest = useMemo(() => buildForest(tasks), [tasks]);
  const allTaskIds = useMemo(() => new Set(tasks.map(t => t.id)), [tasks]);
  const taskMap = useMemo(() => new Map(tasks.map(t => [t.id, t])), [tasks]);
  const prChainMap = useMemo(() => buildPrChainMap(tasks), [tasks]);
  const [groupExpanded, setGroupExpanded] = useState<Record<string, boolean>>({});
  const [activeFilter, setActiveFilter] = useState<FilterType>('All');

  const defaultExpanded = useMemo(() =>
    Object.fromEntries(
      forest.map(r => [
        r.task.id,
        countByStatuses(r, ['IN_PROGRESS', 'PROVISIONING', 'EVALUATING', 'FAILED']) > 0,
      ])
    ),
    [forest]
  );

  const getExpanded = (id: string) => id in groupExpanded ? groupExpanded[id] : (defaultExpanded[id] ?? true);
  const toggleGroup = (id: string) => setGroupExpanded(prev => ({ ...prev, [id]: !getExpanded(id) }));
  const expandAll = () => setGroupExpanded(Object.fromEntries(forest.map(r => [r.task.id, true])));
  const collapseAll = () => setGroupExpanded(Object.fromEntries(forest.map(r => [r.task.id, false])));

  const activeTaskIds = useMemo(
    () => tasks.filter(t => ACTIVE_STATUSES.includes(t.status)).map(t => t.id),
    [tasks],
  );

  const jumpToActive = () => {
    // Expand all root groups that contain active tasks
    const toExpand: Record<string, boolean> = {};
    for (const root of forest) {
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

  if (tasks.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center text-neutral-600 text-sm">
        No tasks to display
      </div>
    );
  }

  const btnClass = 'px-2 py-1 text-xs text-neutral-500 hover:text-neutral-300 bg-neutral-900 border border-neutral-800 rounded transition-colors';
  const chipBase = 'px-2.5 py-1 text-xs rounded-full border transition-colors font-medium';
  const chipActive = 'bg-neutral-700 border-neutral-500 text-neutral-100';
  const chipInactive = 'bg-neutral-900 border-neutral-800 text-neutral-500 hover:text-neutral-300 hover:border-neutral-600';

  return (
    <div className="flex-1 overflow-y-auto overflow-x-auto pb-4 custom-scrollbar">
      <div className="space-y-4 min-w-[400px]">
        <div className="flex items-center gap-1.5">
          {FILTER_CHIPS.map(({ label }) => (
            <button
              key={label}
              className={`${chipBase} ${activeFilter === label ? chipActive : chipInactive}`}
              onClick={() => setActiveFilter(label)}
            >
              {label}
            </button>
          ))}
        </div>
        {forest.length > 1 && (
          <div className="flex items-center gap-2">
            <button className={btnClass} onClick={expandAll}>Expand all</button>
            <button className={btnClass} onClick={collapseAll}>Collapse all</button>
            {activeTaskIds.length > 0 && (
              <button
                className="px-2 py-1 text-xs text-yellow-400 hover:text-yellow-300 bg-yellow-500/10 border border-yellow-500/30 rounded transition-colors"
                onClick={jumpToActive}
              >
                Jump to active
              </button>
            )}
          </div>
        )}
        {forest.map(root => (
          <RootGroup
            key={root.task.id}
            root={root}
            taskMap={taskMap}
            allTaskIds={allTaskIds}
            prChainMap={prChainMap}
            onViewLogs={onViewLogs}
            onNavigateToParent={onNavigateToParent}
            expanded={getExpanded(root.task.id)}
            onToggle={() => toggleGroup(root.task.id)}
            activeFilter={activeFilter}
          />
        ))}
      </div>
    </div>
  );
}

function countNodes(node: TreeNode): number {
  return 1 + node.children.reduce((sum, c) => sum + countNodes(c), 0);
}

function countByStatus(node: TreeNode, status: string): number {
  const self = node.task.status === status ? 1 : 0;
  return self + node.children.reduce((sum, c) => sum + countByStatus(c, status), 0);
}

function countByStatuses(node: TreeNode, statuses: string[]): number {
  const self = statuses.includes(node.task.status) ? 1 : 0;
  return self + node.children.reduce((sum, c) => sum + countByStatuses(c, statuses), 0);
}
