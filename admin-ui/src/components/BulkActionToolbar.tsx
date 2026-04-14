import { CheckSquare, X } from 'lucide-react';
import type { Task } from '../types/task';

interface BulkActionToolbarProps {
  selectedCount: number;
  approvableTasks: Task[];
  cancellableTasks: Task[];
  failedTasks: Task[];
  terminalTasks: Task[];
  onApprove: () => void;
  onCancel: () => void;
  onRetry: () => void;
  onArchive: () => void;
  onExit: () => void;
}

export function BulkActionToolbar({
  selectedCount,
  approvableTasks,
  cancellableTasks,
  failedTasks,
  terminalTasks,
  onApprove,
  onCancel,
  onRetry,
  onArchive,
  onExit,
}: BulkActionToolbarProps) {
  return (
    <div className="fixed bottom-4 left-1/2 -translate-x-1/2 z-40 flex items-center gap-2 bg-neutral-800 border border-neutral-700 rounded-xl px-4 py-2.5 shadow-2xl">
      <span className="flex items-center gap-1.5 text-xs text-neutral-300 font-medium mr-1">
        <CheckSquare className="w-3.5 h-3.5 text-amber-400" />
        {selectedCount} selected
      </span>
      {approvableTasks.length > 0 && (
        <button onClick={onApprove} className="px-3 py-1.5 rounded-lg text-xs font-medium bg-green-500/20 hover:bg-green-500/30 text-green-400 border border-green-500/40 transition-colors">
          Approve {approvableTasks.length}
        </button>
      )}
      {cancellableTasks.length > 0 && (
        <button onClick={onCancel} className="px-3 py-1.5 rounded-lg text-xs font-medium bg-red-500/20 hover:bg-red-500/30 text-red-400 border border-red-500/40 transition-colors">
          Cancel {cancellableTasks.length}
        </button>
      )}
      {failedTasks.length > 0 && (
        <button onClick={onRetry} className="px-3 py-1.5 rounded-lg text-xs font-medium bg-amber-500/20 hover:bg-amber-500/30 text-amber-400 border border-amber-500/40 transition-colors">
          Retry {failedTasks.length}
        </button>
      )}
      {terminalTasks.length > 0 && (
        <button onClick={onArchive} className="px-3 py-1.5 rounded-lg text-xs font-medium bg-neutral-700 hover:bg-neutral-600 text-neutral-300 border border-neutral-600 transition-colors">
          Archive {terminalTasks.length}
        </button>
      )}
      <button onClick={onExit} title="Exit selection mode" className="ml-1 p-1.5 rounded-lg text-neutral-400 hover:text-neutral-200 hover:bg-neutral-700 transition-colors">
        <X className="w-3.5 h-3.5" />
      </button>
    </div>
  );
}
