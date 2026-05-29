import { FAILURE_CLASS_CONFIG } from './TaskCard';
import type { Task } from './TaskCard';
import { parseTimestamp } from '../utils/time';

function renderRetryEta(retryAfter: string | undefined) {
  if (!retryAfter) return null;
  const retryTime = parseTimestamp(retryAfter).getTime();
  const ms = retryTime - Date.now();
  if (ms > 0) {
    const mins = Math.ceil(ms / 60000);
    return <p className="text-xs text-emerald-400">Auto-retrying in {mins}m</p>;
  }
  return <p className="text-xs text-amber-400">Auto-retry pending</p>;
}

export function FailureDetailPanel({ task }: { task: Task }) {
  if (!(task.status === 'FAILED' && task.failure_class && FAILURE_CLASS_CONFIG[task.failure_class])) {
    return null;
  }

  return (
    <div className="bg-neutral-900/50 rounded-lg border border-neutral-800 p-3 mb-2">
      <div className="flex items-center gap-2 mb-1">
        <span className={`inline-block text-[10px] px-1.5 py-0.5 rounded font-semibold ${FAILURE_CLASS_CONFIG[task.failure_class].color}`}>
          {FAILURE_CLASS_CONFIG[task.failure_class].label}
        </span>
        <span className="text-xs text-neutral-400">{FAILURE_CLASS_CONFIG[task.failure_class].suggestion}</span>
      </div>
      {renderRetryEta(task.context?.retry_after as string | undefined)}
    </div>
  );
}
