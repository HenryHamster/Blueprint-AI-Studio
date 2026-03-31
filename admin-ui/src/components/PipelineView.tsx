import { useMemo } from 'react';
import type { Task } from './TaskCard';
import { TASK_TYPE_STYLES, useElapsedTime } from './TaskCard';

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
}

interface PipelineViewProps {
  tasks: Task[];
  onViewLogs: (task: Task) => void;
}

function buildForest(tasks: Task[]): TreeNode[] {
  const taskMap = new Map<string, Task>();
  for (const t of tasks) taskMap.set(t.id, t);

  const nodeMap = new Map<string, TreeNode>();
  for (const t of tasks) nodeMap.set(t.id, { task: t, children: [] });

  const roots: TreeNode[] = [];
  for (const t of tasks) {
    const node = nodeMap.get(t.id)!;
    if (t.parent_task_id && nodeMap.has(t.parent_task_id)) {
      nodeMap.get(t.parent_task_id)!.children.push(node);
    } else {
      roots.push(node);
    }
  }

  // Sort children by pipeline order: research → plan → create → review → fix
  const typeOrder: Record<string, number> = { research: 0, plan: 1, create: 2, review: 3, fix: 4 };
  const sortChildren = (node: TreeNode) => {
    node.children.sort((a, b) => (typeOrder[a.task.task_type || 'create'] ?? 2) - (typeOrder[b.task.task_type || 'create'] ?? 2));
    node.children.forEach(sortChildren);
  };
  roots.forEach(sortChildren);

  // Sort roots by creation time (newest first)
  roots.sort((a, b) => new Date(b.task.created_at).getTime() - new Date(a.task.created_at).getTime());

  return roots;
}

function TaskNode({ task, onViewLogs }: { task: Task; onViewLogs: (task: Task) => void }) {
  const style = task.task_type ? TASK_TYPE_STYLES[task.task_type] : null;
  const Icon = style?.icon;
  const statusColor = STATUS_COLORS[task.status] || 'bg-neutral-500';

  const isActiveForDuration = task.status === 'IN_PROGRESS' || task.status === 'PROVISIONING' || task.status === 'EVALUATING';
  const isTerminal = task.status === 'COMPLETED' || task.status === 'FAILED' || task.status === 'CANCELLED';
  const endTime = isTerminal && task.history && task.history.length > 0
    ? task.history[task.history.length - 1].timestamp
    : isTerminal ? task.created_at : undefined;
  const duration = useElapsedTime(task.created_at, isActiveForDuration ? undefined : endTime);

  return (
    <button
      onClick={() => onViewLogs(task)}
      className="flex items-center gap-2.5 bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-2 hover:border-neutral-600 hover:bg-neutral-800/80 transition-all cursor-pointer text-left w-full min-w-0 group"
      title={`${task.title}\n${task.status} • ${task.task_type || 'create'}\nClick to view logs`}
    >
      {/* Status dot */}
      <div className={`w-2.5 h-2.5 rounded-full shrink-0 ${statusColor}`} />

      {/* Type badge */}
      {style && Icon && (
        <span className={`${style.bg} ${style.text} border text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded font-semibold whitespace-nowrap flex items-center gap-0.5 shrink-0`}>
          <Icon className="w-2.5 h-2.5" />{style.label}
        </span>
      )}

      {/* Title */}
      <span className="text-xs text-neutral-200 truncate flex-1 group-hover:text-neutral-100">
        {task.title}
      </span>

      {/* Duration */}
      <span className="text-[10px] text-neutral-500 font-mono shrink-0">
        {task.status === 'QUEUED' ? 'Waiting' : duration}
      </span>
    </button>
  );
}

function TreeBranch({
  node,
  taskMap,
  allTaskIds,
  onViewLogs,
  isLast,
  depth,
}: {
  node: TreeNode;
  taskMap: Map<string, Task>;
  allTaskIds: Set<string>;
  onViewLogs: (task: Task) => void;
  isLast: boolean;
  depth: number;
}) {
  const depTasks = (node.task.depends_on || [])
    .filter(id => allTaskIds.has(id))
    .map(id => taskMap.get(id)!)
    .filter(Boolean);

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
        {depTasks.length > 0 && (
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
          </div>
        )}

        <TaskNode task={node.task} onViewLogs={onViewLogs} />

        {/* Children */}
        {node.children.length > 0 && (
          <div className="mt-1 space-y-1">
            {node.children.map((child, i) => (
              <TreeBranch
                key={child.task.id}
                node={child}
                taskMap={taskMap}
                allTaskIds={allTaskIds}
                onViewLogs={onViewLogs}
                isLast={i === node.children.length - 1}
                depth={depth + 1}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export function PipelineView({ tasks, onViewLogs }: PipelineViewProps) {
  const forest = useMemo(() => buildForest(tasks), [tasks]);
  const allTaskIds = useMemo(() => new Set(tasks.map(t => t.id)), [tasks]);
  const taskMap = useMemo(() => new Map(tasks.map(t => [t.id, t])), [tasks]);

  if (tasks.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center text-neutral-600 text-sm">
        No tasks to display
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto overflow-x-auto pb-4 custom-scrollbar">
      <div className="space-y-4 min-w-[400px]">
        {forest.map(root => {
          const totalInGroup = countNodes(root);
          const completedInGroup = countByStatus(root, 'COMPLETED');

          return (
            <div
              key={root.task.id}
              className="bg-neutral-900/50 border border-neutral-800/50 rounded-xl p-4"
            >
              {/* Group header */}
              <div className="flex items-center justify-between mb-3 px-1">
                <h3 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest truncate pr-4">
                  {root.task.title}
                </h3>
                <span className="text-[10px] bg-neutral-800 text-neutral-300 px-2 py-0.5 rounded-full font-medium border border-neutral-700 shrink-0">
                  {completedInGroup}/{totalInGroup} done
                </span>
              </div>

              {/* Tree */}
              <div className="space-y-1">
                <TreeBranch
                  node={root}
                  taskMap={taskMap}
                  allTaskIds={allTaskIds}
                  onViewLogs={onViewLogs}
                  isLast={true}
                  depth={0}
                />
              </div>
            </div>
          );
        })}
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
