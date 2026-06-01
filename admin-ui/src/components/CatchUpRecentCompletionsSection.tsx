import { ChevronDown, ChevronUp, CheckCircle } from 'lucide-react';
import { TaskTypeBadge, TASK_TYPE_STYLES } from './TaskCard';
import type { TaskType } from './TaskCard';
import { CompletionRow } from './catchupRows';
import type { RecentCompletion } from './catchupHelpers';

export interface CatchUpRecentCompletionsSectionProps {
  completions: RecentCompletion[];
  expanded: boolean;
  onToggle: () => void;
  onSelectTask: (task: { id: string }) => void;
}

/**
 * "Recent completions" section: a collapsible list of recently completed tasks
 * grouped by task_type (with a type-summary line). Renders nothing when there
 * are no completions. Expand/collapse state is owned by the parent so the
 * panel's auto-expand-on-load logic can drive it.
 */
export function CatchUpRecentCompletionsSection({
  completions,
  expanded,
  onToggle,
  onSelectTask,
}: CatchUpRecentCompletionsSectionProps) {
  if (completions.length === 0) return null;

  // Group by task_type, preserving insertion order
  const groups = new Map<string, RecentCompletion[]>();
  for (const c of completions) {
    const key = c.task_type || 'other';
    let bucket = groups.get(key);
    if (!bucket) { bucket = []; groups.set(key, bucket); }
    bucket.push(c);
  }
  const groupEntries = Array.from(groups.entries());
  const allSameType = groupEntries.length === 1;

  return (
    <div>
      <button
        onClick={onToggle}
        aria-expanded={expanded}
        className="flex items-center gap-1.5 text-[11px] font-bold text-neutral-400 uppercase tracking-widest hover:text-neutral-300 transition-colors mb-1.5"
      >
        {expanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
        <CheckCircle className="w-3 h-3 text-emerald-500" />
        Recent completions ({completions.length})
      </button>
      {expanded && (
        <div className="flex flex-col gap-2">
          {/* Type summary line */}
          <div className="flex flex-wrap items-center gap-1.5">
            {groupEntries.map(([type, items], idx) => {
              const style = TASK_TYPE_STYLES[type];
              return (
                <span key={type} className="flex items-center gap-1 text-[10px]">
                  {idx > 0 && <span className="text-neutral-700">·</span>}
                  <span className={`${style?.bg ?? 'bg-neutral-500/10 border-neutral-500/20'} ${style?.text ?? 'text-neutral-400'} border px-1.5 py-0.5 rounded font-semibold uppercase tracking-wider`}>
                    {items.length} {style?.label ?? type}
                  </span>
                </span>
              );
            })}
          </div>
          {/* Grouped completion rows */}
          {allSameType ? (
            <div className="flex flex-col gap-1.5">
              {completions.map(c => (
                <CompletionRow key={c.task_id} c={c} onSelectTask={onSelectTask} />
              ))}
            </div>
          ) : (
            groupEntries.map(([type, items]) => (
              <div key={type} className="flex flex-col gap-1.5">
                <div className="flex items-center gap-1.5">
                  <TaskTypeBadge taskType={type as TaskType} />
                </div>
                {items.map(c => (
                  <CompletionRow key={c.task_id} c={c} onSelectTask={onSelectTask} />
                ))}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
