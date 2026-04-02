import { Archive } from 'lucide-react';
import { TaskCard } from './TaskCard';
import type { Task, TaskStatus } from './TaskCard';

export interface KanbanColumn {
  id: TaskStatus;
  label: string;
  includes: TaskStatus[];
}

const ARCHIVABLE_COLUMNS: TaskStatus[] = ['COMPLETED', 'CANCELLED', 'FAILED'];

interface KanbanBoardProps {
  tasks: Task[];
  columns: KanbanColumn[];
  loading: boolean;
  maxRetries: number;
  actionLoadingMap: Record<string, string>;
  selectionMode: boolean;
  selectedTaskIds: Set<string>;
  onToggleTaskSelection: (id: string) => void;
  onArchiveColumn: (taskIds: string[], columnLabel: string) => void;
  onApprove: (id: string) => void;
  onRequestChanges: (id: string, feedback: string, action: 'fix' | 'review') => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onEditPrompt: (id: string, newPrompt: string) => void;
  onDismiss: (id: string) => void;
  onArchive: (id: string) => void;
  onViewLogs: (task: Task) => void;
}

export function KanbanBoard({ tasks, columns, loading, maxRetries, actionLoadingMap, selectionMode, selectedTaskIds, onToggleTaskSelection, onArchiveColumn, onApprove, onRequestChanges, onCancel, onRetry, onEditPrompt, onDismiss, onArchive, onViewLogs }: KanbanBoardProps) {
  return (
    <main className="flex justify-center gap-4 overflow-x-auto overflow-y-hidden pb-4 flex-1 custom-scrollbar">
      {columns.map(col => {
        const columnTasks = tasks.filter(t => col.includes.includes(t.status));
        const isArchivable = ARCHIVABLE_COLUMNS.includes(col.id);
        return (
          <div key={col.id} className="flex-1 min-w-[300px] max-w-[400px] flex flex-col bg-neutral-900/50 rounded-xl border border-neutral-800/50 p-3">
            <div className="sticky top-0 z-10 bg-neutral-900/95 backdrop-blur-sm pb-2 -mx-1 px-1 flex items-center justify-between mb-3">
              <h2 className={`text-[11px] font-bold uppercase tracking-widest ${col.id === 'EVALUATING' ? 'text-amber-400' : 'text-neutral-400'}`}>{col.label}</h2>
              <div className="flex items-center gap-1.5">
                <span className="text-[10px] bg-neutral-800 text-neutral-300 px-2 py-0.5 rounded-full font-medium border border-neutral-700">
                  {columnTasks.length}
                </span>
                {isArchivable && columnTasks.length > 0 && (
                  <button
                    onClick={() => onArchiveColumn(columnTasks.map(t => t.id), col.label)}
                    title={`Archive all ${col.label} tasks`}
                    className="p-1 rounded text-neutral-500 hover:text-neutral-300 hover:bg-neutral-700/50 transition-colors"
                  >
                    <Archive className="w-3.5 h-3.5" />
                  </button>
                )}
              </div>
            </div>

            <div className="overflow-y-auto flex-1 space-y-3 pr-1 custom-scrollbar">
              {columnTasks.map(task => (
                <div key={task.id} className="relative">
                  {selectionMode && (
                    <div className="absolute top-2 left-2 z-10">
                      <input
                        type="checkbox"
                        checked={selectedTaskIds.has(task.id)}
                        onChange={() => onToggleTaskSelection(task.id)}
                        className="w-4 h-4 accent-amber-400 cursor-pointer"
                      />
                    </div>
                  )}
                  <div className={selectionMode ? 'pl-6' : ''}>
                    <TaskCard
                      task={task}
                      maxRetries={maxRetries}
                      actionLoading={actionLoadingMap[task.id]}
                      onApprove={onApprove}
                      onRequestChanges={onRequestChanges}
                      onCancel={onCancel}
                      onRetry={onRetry}
                      onEditPrompt={onEditPrompt}
                      onDismiss={onDismiss}
                      onArchive={onArchive}
                      onViewLogs={onViewLogs}
                    />
                  </div>
                </div>
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
