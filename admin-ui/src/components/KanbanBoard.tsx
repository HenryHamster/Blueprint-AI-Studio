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
  loading: boolean;
  maxRetries: number;
  actionLoadingMap: Record<string, string>;
  hideEmptyColumns?: boolean;
  onApprove: (id: string) => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onEditPrompt: (id: string, newPrompt: string) => void;
  onDismiss: (id: string) => void;
  onViewLogs: (task: Task) => void;
}

export function KanbanBoard({ tasks, columns, loading, maxRetries, actionLoadingMap, hideEmptyColumns = true, onApprove, onCancel, onRetry, onEditPrompt, onDismiss, onViewLogs }: KanbanBoardProps) {
  return (
    <main className="flex justify-center gap-4 overflow-x-auto overflow-y-hidden pb-4 flex-1 custom-scrollbar">
      {columns.map(col => {
        const columnTasks = tasks.filter(t => col.includes.includes(t.status));
        if (hideEmptyColumns && columnTasks.length === 0) return null;
        return (
          <div key={col.id} className="flex-1 min-w-[300px] max-w-[400px] flex flex-col bg-neutral-900/50 rounded-xl border border-neutral-800/50 p-3">
            <div className="sticky top-0 z-10 bg-neutral-900/95 backdrop-blur-sm pb-2 -mx-1 px-1 flex items-center justify-between mb-3">
              <h2 className={`text-[11px] font-bold uppercase tracking-widest ${col.id === 'EVALUATING' ? 'text-blue-400' : 'text-neutral-400'}`}>{col.label}</h2>
              <span className="text-[10px] bg-neutral-800 text-neutral-300 px-2 py-0.5 rounded-full font-medium border border-neutral-700">
                {columnTasks.length}
              </span>
            </div>

            <div className="overflow-y-auto flex-1 space-y-3 pr-1 custom-scrollbar">
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
