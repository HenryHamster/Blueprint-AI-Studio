import type { DirectionProgress } from './DirectionProgressSection';

interface RecurringErrorCluster {
  fingerprint: string;
  error_snippet: string;
  count: number;
  direction_ids: string[];
  task_types: string[];
  first_task_id: string;
}

interface EarlyWarning {
  failure_class: string;
  rate_progression?: number[] | null;
  affected_direction_count: number;
  severity?: string | null;
  suggested_action?: string | null;
}

interface RecurringPattern {
  fingerprint: string;
  example_message?: string | null;
  occurrence_count: number;
  affected_direction_count: number;
  remediation_status: 'fix_verified' | 'fix_shipped' | 'investigating' | 'unaddressed';
  severity?: string | null;
  proactive?: boolean;
}

interface CatchUpFailureSignalsSectionProps {
  recurringErrorClusters: RecurringErrorCluster[];
  earlyWarnings: EarlyWarning[];
  recurringPatterns: RecurringPattern[];
  directionProgress: DirectionProgress[];
  onSelectTask: (task: { id: string }) => void;
}

export function CatchUpFailureSignalsSection({
  recurringErrorClusters,
  earlyWarnings,
  recurringPatterns,
  directionProgress,
  onSelectTask,
}: CatchUpFailureSignalsSectionProps) {
  if (recurringErrorClusters.length === 0 && earlyWarnings.length === 0 && recurringPatterns.length === 0) {
    return null;
  }

  const dirTextById = new Map<string, string>(
    directionProgress.map(dp => [dp.direction_id, dp.direction_text || dp.direction_id]),
  );

  return (
    <>
      {/* Systemic errors — clusters of failures with matching error fingerprints */}
      {recurringErrorClusters.length > 0 && (
        <div className="flex flex-col gap-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Systemic errors</span>
            <span className="text-[10px] text-neutral-400">
              {recurringErrorClusters.length} cluster{recurringErrorClusters.length !== 1 ? 's' : ''}
            </span>
          </div>
          <div className="flex flex-col gap-1 ml-1 max-w-full">
            {recurringErrorClusters.map(cluster => {
              const snippet = cluster.error_snippet.length > 120
                ? cluster.error_snippet.slice(0, 120) + '…'
                : cluster.error_snippet;
              const dirCount = cluster.direction_ids.length;
              const dirLabels = cluster.direction_ids
                .map(id => dirTextById.get(id))
                .filter((s): s is string => !!s)
                .map(s => s.length > 30 ? s.slice(0, 30) + '…' : s);
              return (
                <div
                  key={cluster.fingerprint}
                  role="button"
                  tabIndex={0}
                  className="flex flex-col gap-0.5 text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded cursor-pointer hover:bg-neutral-800/50 min-h-[44px]"
                  onClick={() => onSelectTask({ id: cluster.first_task_id })}
                  onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelectTask({ id: cluster.first_task_id }); } }}
                >
                  <div className="flex items-center gap-2">
                    <span className="flex-1 truncate text-neutral-400 font-mono text-[10px]">{snippet}</span>
                    <span className="shrink-0 text-[9px] font-semibold px-1.5 py-0.5 rounded bg-red-500/10 text-red-400 border border-red-500/20">
                      ×{cluster.count}
                    </span>
                    <span
                      className="shrink-0 text-[10px] text-cyan-400 hover:text-cyan-300"
                      onClick={e => { e.stopPropagation(); onSelectTask({ id: cluster.first_task_id }); }}
                      title={`Open first task ${cluster.first_task_id}`}
                    >
                      view →
                    </span>
                  </div>
                  <div className="flex flex-wrap items-center gap-1 text-[10px] text-neutral-500">
                    {dirCount > 1 && (
                      <span className="text-amber-400">across {dirCount} directions</span>
                    )}
                    {dirLabels.slice(0, 2).map((label) => (
                      <span key={label} className="text-[9px] text-neutral-500 truncate max-w-[180px]">
                        · {label}
                      </span>
                    ))}
                    {cluster.task_types.map(tt => (
                      <span key={tt} className="text-[9px] bg-neutral-700/30 text-neutral-400 border border-neutral-700/30 px-1 py-0.5 rounded uppercase tracking-wider">
                        {tt}
                      </span>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Early warnings — failure-rate momentum pre-anomaly signals */}
      {earlyWarnings.length > 0 && (
        <div className="flex flex-col gap-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] font-bold text-amber-400 uppercase tracking-widest">⚠ Early warnings</span>
            <span className="text-[10px] text-neutral-400">
              {earlyWarnings.length} rising trend{earlyWarnings.length !== 1 ? 's' : ''}
            </span>
          </div>
          <div className="flex flex-col gap-1 ml-1">
            {earlyWarnings.map((w, idx) => {
              const progression = (w.rate_progression ?? [])
                .map(r => `${Math.round(r * 100)}%`)
                .join(' → ');
              return (
                <div
                  key={`${w.failure_class}-${idx}`}
                  className="flex flex-col gap-0.5 text-xs bg-amber-500/5 border border-amber-500/30 px-2 py-1.5 rounded"
                >
                  <div className="flex items-center gap-2">
                    <span className="shrink-0 text-[9px] font-semibold px-1.5 py-0.5 rounded border uppercase tracking-wider bg-amber-500/10 text-amber-400 border-amber-500/20">
                      rising
                    </span>
                    <span className="flex-1 truncate text-amber-200 font-mono text-[10px]">{w.failure_class}</span>
                    {progression && (
                      <span className="shrink-0 text-[10px] font-semibold text-amber-300 font-mono">
                        {progression}
                      </span>
                    )}
                  </div>
                  <div className="flex flex-wrap items-center gap-1 text-[10px] text-neutral-500">
                    {w.affected_direction_count > 0 && (
                      <span>across {w.affected_direction_count} direction{w.affected_direction_count !== 1 ? 's' : ''}</span>
                    )}
                    {w.severity && (
                      <span className="text-[9px] bg-neutral-700/30 text-neutral-400 border border-neutral-700/30 px-1 py-0.5 rounded uppercase tracking-wider">
                        {w.severity}
                      </span>
                    )}
                    {w.suggested_action && (
                      <span className="text-neutral-400">→ {w.suggested_action}</span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Recurring pattern detection — detection-to-fix lifecycle */}
      {recurringPatterns.length > 0 && (
        <div className="flex flex-col gap-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Recurring patterns</span>
            <span className="text-[10px] text-neutral-400">
              {recurringPatterns.length} pattern{recurringPatterns.length !== 1 ? 's' : ''}
            </span>
          </div>
          <div className="flex flex-col gap-1 ml-1">
            {recurringPatterns.map(pattern => {
              const statusStyles: Record<string, string> = {
                fix_verified: 'bg-green-500/10 text-green-400 border-green-500/20',
                fix_shipped: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
                investigating: 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20',
                unaddressed: 'bg-red-500/10 text-red-400 border-red-500/20',
              };
              const statusClass = statusStyles[pattern.remediation_status] ?? 'bg-neutral-700/30 text-neutral-400 border-neutral-700/30';
              const snippet = (pattern.example_message ?? '').length > 120
                ? (pattern.example_message ?? '').slice(0, 120) + '…'
                : (pattern.example_message ?? '');
              return (
                <div
                  key={pattern.fingerprint}
                  className="flex flex-col gap-0.5 text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded"
                >
                  <div className="flex items-center gap-2">
                    <span className={`shrink-0 text-[9px] font-semibold px-1.5 py-0.5 rounded border uppercase tracking-wider ${statusClass}`}>
                      {pattern.remediation_status.replace('_', ' ')}
                    </span>
                    <span className="flex-1 truncate text-neutral-400 font-mono text-[10px]">{snippet}</span>
                    <span className="shrink-0 text-[9px] font-semibold px-1.5 py-0.5 rounded bg-neutral-800/50 text-neutral-300 border border-neutral-700/30">
                      ×{pattern.occurrence_count}
                    </span>
                  </div>
                  <div className="flex flex-wrap items-center gap-1 text-[10px] text-neutral-500">
                    {pattern.affected_direction_count > 0 && (
                      <span>across {pattern.affected_direction_count} direction{pattern.affected_direction_count !== 1 ? 's' : ''}</span>
                    )}
                    {pattern.severity && (
                      <span className="text-[9px] bg-neutral-700/30 text-neutral-400 border border-neutral-700/30 px-1 py-0.5 rounded uppercase tracking-wider">
                        {pattern.severity}
                      </span>
                    )}
                    {pattern.proactive && (
                      <span className="text-[9px] bg-purple-500/10 text-purple-400 border border-purple-500/20 px-1 py-0.5 rounded uppercase tracking-wider">
                        proactive
                      </span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </>
  );
}
