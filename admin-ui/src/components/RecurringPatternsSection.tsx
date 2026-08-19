interface RecurringPattern {
  fingerprint: string;
  occurrence_count: number;
  severity?: string | null;
  example_message?: string | null;
  affected_direction_count: number;
  affected_directions?: { direction_id: string; direction_text: string }[];
  remediation_status: 'fix_verified' | 'fix_shipped' | 'investigating' | 'unaddressed';
  proactive?: boolean;
}

interface RecurringPatternsSectionProps {
  recurringPatterns: RecurringPattern[];
  onDirectionClick?: (directionId: string) => void;
}

const STATUS_STYLES: Record<string, string> = {
  fix_verified: 'bg-green-500/10 text-green-400 border-green-500/20',
  fix_shipped: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
  investigating: 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20',
  unaddressed: 'bg-red-500/10 text-red-400 border-red-500/20',
};

export function RecurringPatternsSection({ recurringPatterns, onDirectionClick }: RecurringPatternsSectionProps) {
  if (recurringPatterns.length === 0) {
    return null;
  }

  return (
    <section>
      <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">
        Recurring Patterns <span className="text-neutral-600 font-normal normal-case tracking-normal">· {recurringPatterns.length}</span>
      </h2>
      <div className="flex flex-col gap-1">
        {recurringPatterns.slice(0, 5).map(pattern => {
          const statusClass = STATUS_STYLES[pattern.remediation_status] ?? 'bg-workbench-layer/30 text-neutral-400 border-neutral-700/30';
          const snippet = (pattern.example_message ?? '').length > 120
            ? (pattern.example_message ?? '').slice(0, 120) + '…'
            : (pattern.example_message ?? '');
          const dirs = pattern.affected_directions ?? [];
          return (
            <div
              key={pattern.fingerprint}
              className="flex flex-col gap-0.5 text-xs bg-workbench-base/50 border border-neutral-800/50 px-2 py-1.5 rounded"
            >
              <div className="flex items-center gap-2">
                <span className={`shrink-0 text-ui-xs font-semibold px-1.5 py-0.5 rounded border uppercase tracking-wider ${statusClass}`}>
                  {pattern.remediation_status.replace('_', ' ')}
                </span>
                <span className="flex-1 truncate text-neutral-400 font-mono text-ui-sm">{snippet}</span>
                <span className="shrink-0 text-ui-xs font-semibold px-1.5 py-0.5 rounded bg-workbench-panel/50 text-neutral-300 border border-neutral-700/30">
                  ×{pattern.occurrence_count}
                </span>
              </div>
              {dirs.length > 0 && (
                <div className="flex flex-wrap items-center gap-1 text-ui-sm text-neutral-500">
                  {dirs.slice(0, 4).map(d => (
                    // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
                    <button
                      key={d.direction_id}
                      type="button"
                      onClick={() => onDirectionClick?.(d.direction_id)}
                      className="text-ui-sm px-1 py-0.5 rounded bg-workbench-panel/50 text-neutral-300 border border-neutral-700/30 hover:border-neutral-600 truncate max-w-[200px]"
                      title={d.direction_text || d.direction_id}
                    >
                      {(d.direction_text || d.direction_id).slice(0, 40)}
                    </button>
                  ))}
                  {dirs.length > 4 && <span>+{dirs.length - 4} more</span>}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
