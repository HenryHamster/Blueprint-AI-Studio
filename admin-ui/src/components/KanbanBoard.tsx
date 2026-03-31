import { TaskCard } from './TaskCard';
import type { Task, TaskStatus } from './TaskCard';

export interface KanbanColumn {
  id: TaskStatus;
  label: string;
  includes: TaskStatus[];
}

interface KanbanBoardProps {
  tasks: Task[];
  columns: KanbanColumn[];
  selectedProjectId: string | null;
  loading: boolean;
  maxRetries: number;
  actionLoadingMap: Record<string, string>;
  onApprove: (id: string) => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onEditPrompt: (id: string, newPrompt: string) => void;
  onDismiss: (id: string) => void;
  onViewLogs: (task: Task) => void;
}

export function KanbanBoard({ tasks, columns, selectedProjectId, loading, maxRetries, actionLoadingMap, onApprove, onCancel, onRetry, onEditPrompt, onDismiss, onViewLogs }: KanbanBoardProps) {
  return (
    <main className="flex gap-4 overflow-x-auto overflow-y-hidden pb-4 flex-1 custom-scrollbar">
      {columns.map(col => {
        const columnTasks = tasks.filter(t => {
          const statusMatch = col.includes.includes(t.status);
          const projectMatch = selectedProjectId === null || t.project_id === selectedProjectId;
          return statusMatch && projectMatch;
        });
        return (
          <div key={col.id} className="flex-1 min-w-[300px] flex flex-col bg-neutral-900/50 rounded-xl border border-neutral-800/50 p-3">
            <div className="flex items-center justify-between mb-3 px-1">
              <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest">{col.label}</h2>
              <span className="text-[10px] bg-neutral-800 text-neutral-300 px-2 py-0.5 rounded-full font-medium border border-neutral-700">
                {columnTasks.length}
              </span>
            </div>

            <div className="flex-1 overflow-y-auto space-y-3 pr-1 custom-scrollbar">
              {columnTasks.map(task => (
                <TaskCard
                  key={task.id}
                  task={task}
                  maxRetries={maxRetries}
                  actionLoading={actionLoadingMap[task.id]}
                  onApprove={onApprove}
                  onCancel={onCancel}
                  onRetry={onRetry}
                  onEditPrompt={onEditPrompt}
                  onDismiss={onDismiss}
                  onViewLogs={onViewLogs}
                />
              ))}
              {columnTasks.length === 0 && !loading && (
                <div className="text-center text-neutral-600 text-xs py-8 border border-dashed border-neutral-800 rounded-xl bg-neutral-900/20">
                  No tasks
                </div>
              )}
            </div>
          </div>
        );
      })}
    </main>
  );
}
