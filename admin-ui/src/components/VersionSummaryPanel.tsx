import type { JSX } from 'react';
import type { ChangelogEntry } from '../api';
import { isSafeUrl } from './TaskCard';

// Group changelog entries by task_type, preserving newest-first order within each group.
function groupByType(entries: ChangelogEntry[]): Map<string, ChangelogEntry[]> {
  const groups = new Map<string, ChangelogEntry[]>();
  for (const entry of entries) {
    const key = entry.task_type ?? 'other';
    const group = groups.get(key);
    if (group) {
      group.push(entry);
    } else {
      groups.set(key, [entry]);
    }
  }
  return groups;
}

function TaskRow({ entry }: { entry: ChangelogEntry }): JSX.Element {
  const prRef = entry.pr_number != null ? (
    entry.pr_url && isSafeUrl(entry.pr_url) ? (
      <a
        href={entry.pr_url}
        target="_blank"
        rel="noopener noreferrer"
        className="font-mono text-[10px] text-slate-500 hover:text-slate-300 shrink-0 pt-px"
      >
        #{entry.pr_number}
      </a>
    ) : (
      <span className="font-mono text-[10px] text-slate-500 shrink-0 pt-px">
        #{entry.pr_number}
      </span>
    )
  ) : null;

  return (
    <li className="flex items-start gap-2 py-0.5">
      {prRef}
      <span className="text-xs text-slate-300 break-words min-w-0">{entry.title}</span>
    </li>
  );
}

function TypeSection({
  taskType,
  entries,
}: {
  taskType: string;
  entries: ChangelogEntry[];
}): JSX.Element {
  return (
    <div className="mb-3 last:mb-0">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-500 mb-1">
        {taskType} <span className="text-slate-600 font-normal">({entries.length})</span>
      </div>
      <ul className="space-y-0.5 pl-1">
        {entries.map((entry, i) => (
          <TaskRow key={i} entry={entry} />
        ))}
      </ul>
    </div>
  );
}

export function VersionSummaryPanel({
  tasksMerged,
}: {
  tasksMerged: ChangelogEntry[] | undefined;
}): JSX.Element | null {
  if (tasksMerged === undefined) return null;

  if (tasksMerged.length === 0) {
    return (
      <div
        data-testid="version-summary-panel-empty"
        className="mt-3 rounded border border-slate-700/60 bg-slate-800/40 px-3 py-2"
      >
        <div className="text-[11px] text-slate-500 italic">No tasks merged into this version yet.</div>
      </div>
    );
  }

  const groups = groupByType(tasksMerged);

  return (
    <div
      data-testid="version-summary-panel"
      className="mt-3 rounded border border-slate-700/60 bg-slate-800/40 px-3 py-2"
    >
      <div className="text-[11px] font-semibold text-slate-400 mb-2">
        What shipped ({tasksMerged.length} task{tasksMerged.length !== 1 ? 's' : ''})
      </div>
      <div className="max-h-48 overflow-y-auto pr-1">
        {Array.from(groups.entries()).map(([type, entries]) => (
          <TypeSection key={type} taskType={type} entries={entries} />
        ))}
      </div>
    </div>
  );
}
