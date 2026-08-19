import { CheckSquare, X } from 'lucide-react';
import type { Task } from '../types/task';
import { buildBulkCancelConsequence } from './taskCancelCopy';

interface ConfirmDialogConfig {
  title: string;
  message: string;
  confirmLabel: string;
  onConfirm: () => void;
}

interface BulkActionBarProps {
  selectedCount: number;
  approvableTasks: Task[];
  cancellableTasks: Task[];
  failedTasks: Task[];
  terminalTasks: Task[];
  setConfirmDialog: (d: ConfirmDialogConfig | null) => void;
  handleBulkAction: (action: 'approve' | 'retry', taskIds: string[]) => void;
  handleBulkCancel: (taskIds: string[]) => void;
  handleBulkArchiveColumn: (taskIds: string[]) => void;
  toggleSelectionMode: () => void;
}

function buildBulkConfirmList(tasks: Task[]): string {
  const titles = tasks.map(t => t.title || t.id.slice(0, 12));
  if (titles.length <= 5) return titles.join('\n');
  return titles.slice(0, 5).join('\n') + `\n...and ${titles.length - 5} more`;
}

// Prepend a one-line consequence summary above the affected-task list so the
// operator sees the scoped impact before confirming a bulk mutation.
function buildBulkConfirmMessage(tasks: Task[], consequence?: string): string {
  const list = buildBulkConfirmList(tasks);
  return consequence ? `${consequence}\n\n${list}` : list;
}

export function BulkActionBar({
  selectedCount,
  approvableTasks,
  cancellableTasks,
  failedTasks,
  terminalTasks,
  setConfirmDialog,
  handleBulkAction,
  handleBulkCancel,
  handleBulkArchiveColumn,
  toggleSelectionMode,
}: BulkActionBarProps) {
  return (
    <div className="fixed bottom-4 left-1/2 -translate-x-1/2 z-40 flex flex-wrap items-center justify-center gap-2 bg-workbench-panel border border-neutral-700 rounded-lg px-4 py-2.5 shadow-2xl max-w-[calc(100vw-1rem)]">
      <span className="flex items-center gap-1.5 text-xs text-neutral-300 font-medium mr-1">
        <CheckSquare className="w-3.5 h-3.5 text-amber-400" />
        {selectedCount} selected
      </span>
      {approvableTasks.length > 0 && (
        <button
          onClick={() => setConfirmDialog({
            title: `Approve ${approvableTasks.length} task${approvableTasks.length !== 1 ? 's' : ''}?`,
            message: buildBulkConfirmMessage(approvableTasks),
            confirmLabel: `Approve ${approvableTasks.length}`,
            onConfirm: () => {
              handleBulkAction('approve', approvableTasks.map(t => t.id));
              toggleSelectionMode();
              setConfirmDialog(null);
            },
          })}
          className="px-3 py-1.5 min-h-[44px] rounded-lg text-xs font-medium bg-green-500/20 hover:bg-green-500/30 text-green-400 border border-green-500/40 transition-colors"
        >
          Approve {approvableTasks.length}
        </button>
      )}
      {cancellableTasks.length > 0 && (
        <button
          onClick={() => setConfirmDialog({
            title: `Cancel ${cancellableTasks.length} task${cancellableTasks.length !== 1 ? 's' : ''}?`,
            message: buildBulkConfirmMessage(cancellableTasks, buildBulkCancelConsequence(cancellableTasks)),
            confirmLabel: `Cancel ${cancellableTasks.length}`,
            onConfirm: () => handleBulkCancel(cancellableTasks.map(t => t.id)),
          })}
          className="px-3 py-1.5 min-h-[44px] rounded-lg text-xs font-medium bg-red-500/20 hover:bg-red-500/30 text-red-400 border border-red-500/40 transition-colors"
        >
          Cancel {cancellableTasks.length}
        </button>
      )}
      {failedTasks.length > 0 && (
        <button
          onClick={() => setConfirmDialog({
            title: `Retry ${failedTasks.length} task${failedTasks.length !== 1 ? 's' : ''}?`,
            message: buildBulkConfirmMessage(failedTasks),
            confirmLabel: `Retry ${failedTasks.length}`,
            onConfirm: () => {
              handleBulkAction('retry', failedTasks.map(t => t.id));
              toggleSelectionMode();
              setConfirmDialog(null);
            },
          })}
          className="px-3 py-1.5 min-h-[44px] rounded-lg text-xs font-medium bg-amber-500/20 hover:bg-amber-500/30 text-amber-400 border border-amber-500/40 transition-colors"
        >
          Retry {failedTasks.length}
        </button>
      )}
      {terminalTasks.length > 0 && (
        <button
          onClick={() => setConfirmDialog({
            title: `Archive ${terminalTasks.length} task${terminalTasks.length !== 1 ? 's' : ''}?`,
            message: buildBulkConfirmMessage(terminalTasks, 'Moves these tasks off the board to the archive. Recoverable via Unarchive.'),
            confirmLabel: `Archive ${terminalTasks.length}`,
            onConfirm: () => {
              handleBulkArchiveColumn(terminalTasks.map(t => t.id));
              toggleSelectionMode();
            },
          })}
          className="px-3 py-1.5 min-h-[44px] rounded-lg text-xs font-medium bg-workbench-layer hover:bg-workbench-hover text-neutral-300 border border-neutral-600 transition-colors"
        >
          Archive {terminalTasks.length}
        </button>
      )}
      <button
        onClick={toggleSelectionMode}
        title="Exit selection mode"
        className="ml-1 min-h-[44px] min-w-[44px] flex items-center justify-center rounded-lg text-neutral-400 hover:text-neutral-200 hover:bg-workbench-hover transition-colors"
      >
        <X className="w-3.5 h-3.5" />
      </button>
    </div>
  );
}
