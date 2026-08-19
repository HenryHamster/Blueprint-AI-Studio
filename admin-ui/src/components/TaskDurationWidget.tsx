const SCOPE_ORDER = ['small', 'medium', 'large'] as const;
const PHASE_ORDER = ['provisioning', 'execution'] as const;

// Amber threshold: execution p50 > 30 minutes
const EXEC_P50_WARN_SECONDS = 30 * 60;

function fmt(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

export interface TaskDurationStats {
  stats: Record<string, { p50_seconds: number; sample_count: number }>;
  scope_duration_stats: Record<string, { p50_seconds: number; p90_seconds: number; sample_size: number }>;
  phase_duration_stats: Record<string, { p50_seconds: number; sample_size: number }>;
}

interface Props {
  data: TaskDurationStats | null | undefined;
}

export function TaskDurationWidget({ data }: Props) {
  if (!data) return null;
  const hasTypes = Object.keys(data.stats).length > 0;
  const hasScopes = Object.keys(data.scope_duration_stats).length > 0;
  const hasPhases = Object.keys(data.phase_duration_stats).length > 0;
  if (!hasTypes && !hasScopes && !hasPhases) return null;

  return (
    <section>
      <div className="flex flex-wrap gap-1.5 items-center">
        <span className="text-ui-sm text-neutral-500 uppercase tracking-wide mr-1">Duration p50</span>

        {/* Task types */}
        {hasTypes && Object.entries(data.stats).sort(([a], [b]) => a.localeCompare(b)).map(([type, s]) => (
          <span
            key={type}
            className="inline-flex items-center gap-1 text-ui-sm font-semibold px-2 py-0.5 rounded border bg-workbench-panel/60 text-neutral-300 border-neutral-600/40"
            title={`${type}: p50 ${fmt(s.p50_seconds)} (n=${s.sample_count})`}
          >
            {type}
            <span className="font-bold text-neutral-200">{fmt(s.p50_seconds)}</span>
          </span>
        ))}

        {/* Scope breakdown */}
        {hasScopes && (
          <>
            <span className="text-ui-sm text-neutral-600 mx-0.5">|</span>
            <span className="text-ui-sm text-neutral-500 uppercase tracking-wide">by scope</span>
            {SCOPE_ORDER.filter(sc => data.scope_duration_stats[sc]).map(sc => {
              const s = data.scope_duration_stats[sc];
              return (
                <span
                  key={sc}
                  className="inline-flex items-center gap-1 text-ui-sm font-semibold px-2 py-0.5 rounded border bg-blue-500/10 text-blue-300 border-blue-500/25"
                  title={`${sc}: p50 ${fmt(s.p50_seconds)}, p90 ${fmt(s.p90_seconds)} (n=${s.sample_size})`}
                >
                  {sc}
                  <span className="font-bold">{fmt(s.p50_seconds)}</span>
                  <span className="text-blue-400/60">/{fmt(s.p90_seconds)}</span>
                </span>
              );
            })}
          </>
        )}

        {/* Phase breakdown */}
        {hasPhases && (
          <>
            <span className="text-ui-sm text-neutral-600 mx-0.5">|</span>
            <span className="text-ui-sm text-neutral-500 uppercase tracking-wide">vm</span>
            {PHASE_ORDER.filter(ph => data.phase_duration_stats[ph]).map(ph => {
              const s = data.phase_duration_stats[ph];
              const isWarn = ph === 'execution' && s.p50_seconds > EXEC_P50_WARN_SECONDS;
              const cls = isWarn
                ? 'bg-amber-500/20 text-amber-300 border-amber-500/30'
                : 'bg-workbench-panel/60 text-neutral-300 border-neutral-600/40';
              return (
                <span
                  key={ph}
                  className={`inline-flex items-center gap-1 text-ui-sm font-semibold px-2 py-0.5 rounded border ${cls}`}
                  title={`VM ${ph}: p50 ${fmt(s.p50_seconds)} (n=${s.sample_size})${isWarn ? ' — slow execution' : ''}`}
                >
                  {ph === 'provisioning' ? 'prov' : 'exec'}
                  <span className="font-bold">{fmt(s.p50_seconds)}</span>
                </span>
              );
            })}
          </>
        )}
      </div>
    </section>
  );
}
