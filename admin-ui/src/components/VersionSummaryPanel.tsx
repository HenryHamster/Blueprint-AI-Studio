import { useState, type JSX } from 'react';
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

function exportChangelogMarkdown(entries: ChangelogEntry[], heading?: string): string {
  if (entries.length === 0) return 'No tasks merged into this version.\n';
  const lines: string[] = [];
  if (heading) lines.push(`## ${heading}\n`);
  const groups = groupByType(entries);
  for (const [type, group] of groups.entries()) {
    lines.push(`### ${type} (${group.length})`);
    for (const entry of group) {
      const pr = entry.pr_number != null ? `#${entry.pr_number} ` : '';
      lines.push(`- ${pr}${entry.title ?? ''}`);
    }
    lines.push('');
  }
  return lines.join('\n');
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
  defaultOpen,
}: {
  taskType: string;
  entries: ChangelogEntry[];
  defaultOpen?: boolean;
}): JSX.Element {
  const [open, setOpen] = useState(defaultOpen ?? true);

  return (
    <div className="mb-2 last:mb-0">
      <button
        onClick={() => setOpen((v) => !v)}
        data-testid={`type-section-toggle-${taskType}`}
        className="flex items-center gap-1 w-full text-left min-h-[44px] py-0.5 group"
        aria-expanded={open}
      >
        <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-500 group-hover:text-slate-400 transition-colors">
          {taskType}
        </span>
        <span className="text-slate-600 font-normal text-[10px]">({entries.length})</span>
        <span className="ml-auto text-[10px] text-slate-600 group-hover:text-slate-400 transition-colors">
          {open ? '▾' : '▸'}
        </span>
      </button>
      {open && (
        <ul className="space-y-0.5 pl-1 pb-1">
          {entries.map((entry, i) => (
            <TaskRow key={i} entry={entry} />
          ))}
        </ul>
      )}
    </div>
  );
}

function TypeCountBadge({ taskType, count }: { taskType: string; count: number }): JSX.Element {
  return (
    <span className="inline-flex items-center gap-0.5 rounded bg-slate-700/60 px-1.5 py-0.5 text-[10px] text-slate-400">
      <span className="font-medium">{count}</span>
      <span className="text-slate-500">{taskType}</span>
    </span>
  );
}

const BUCKET_META: Record<string, { label: string; className: string }> = {
  undeclared:                { label: 'no criteria',    className: 'text-amber-400' },
  declared_unsatisfied:      { label: 'criteria failed', className: 'text-red-400' },
  satisfied_unproven:        { label: 'satisfied',       className: 'text-slate-400' },
  merged_unverified_degraded:{ label: 'degraded',        className: 'text-amber-400' },
  malformed:                 { label: 'malformed',       className: 'text-red-400' },
};

function AcceptanceBucketBreakdown({ aggregate }: { aggregate: Record<string, number> }): JSX.Element | null {
  const entries = Object.entries(BUCKET_META)
    .map(([key, meta]) => ({ key, count: aggregate[key] ?? 0, ...meta }))
    .filter(e => e.count > 0);
  if (entries.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] mb-2" data-testid="acceptance-bucket-breakdown">
      {entries.map(e => (
        <span key={e.key} className={e.className}>
          {e.count} {e.label}
        </span>
      ))}
    </div>
  );
}

export function VersionSummaryPanel({
  tasksMerged,
  heading,
  acceptanceAggregate,
}: {
  tasksMerged: ChangelogEntry[] | undefined;
  heading?: string;
  acceptanceAggregate?: Record<string, number> | null;
}): JSX.Element | null {
  const [copied, setCopied] = useState(false);

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

  const handleCopyMarkdown = () => {
    const md = exportChangelogMarkdown(tasksMerged, heading ?? 'What shipped');
    navigator.clipboard.writeText(md).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }).catch(() => {});
  };

  // Default: collapse sections when total tasks > 20 to keep the panel scannable
  const defaultOpen = tasksMerged.length <= 20;

  return (
    <div
      data-testid="version-summary-panel"
      className="mt-3 rounded border border-slate-700/60 bg-slate-800/40 px-3 py-2"
    >
      <div className="flex items-center justify-between mb-2">
        <div className="text-[11px] font-semibold text-slate-400">
          {heading ?? 'What shipped'} ({tasksMerged.length} task{tasksMerged.length !== 1 ? 's' : ''})
        </div>
        <button
          onClick={handleCopyMarkdown}
          data-testid="copy-markdown-button"
          className="text-[10px] text-slate-500 hover:text-slate-300 transition-colors min-h-[44px] min-w-[44px] flex items-center justify-center"
          title="Copy changelog as Markdown"
        >
          {copied ? 'Copied!' : 'Copy markdown'}
        </button>
      </div>
      {acceptanceAggregate && <AcceptanceBucketBreakdown aggregate={acceptanceAggregate} />}
      {/* Per-type count summary — lets operator grasp shape of release at a glance */}
      <div className="flex flex-wrap gap-1 mb-2" data-testid="type-count-summary">
        {Array.from(groups.entries()).map(([type, entries]) => (
          <TypeCountBadge key={type} taskType={type} count={entries.length} />
        ))}
      </div>
      <div className="max-h-64 overflow-y-auto pr-1">
        {Array.from(groups.entries()).map(([type, entries]) => (
          <TypeSection key={type} taskType={type} entries={entries} defaultOpen={defaultOpen} />
        ))}
      </div>
    </div>
  );
}
