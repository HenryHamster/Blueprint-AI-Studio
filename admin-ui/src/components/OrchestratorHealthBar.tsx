interface OrchestratorHealth {
  last_cycle_seconds_ago: number | null;
  queued_tasks: number;
  active_tasks: number;
  pool_info: Record<string, unknown>;
  deploy_monitoring?: {
    external_health?: Record<string, boolean | string | string[]>;
  };
}

interface OrchestratorHealthBarProps {
  health: OrchestratorHealth | null;
}

const DEP_LABELS: Record<string, string> = {
  github_api: 'GitHub',
  db: 'DB',
  gcp: 'GCP',
};

function statusColor(val: boolean | string): string {
  if (typeof val === 'boolean') return val ? 'bg-emerald-500' : 'bg-red-500';
  const s = val.toLowerCase();
  if (s === 'ok' || s === 'healthy') return 'bg-emerald-500';
  if (s === 'degraded' || s === 'slow') return 'bg-amber-400';
  return 'bg-red-500';
}

function statusDotTitle(key: string, val: boolean | string): string {
  const label = DEP_LABELS[key] ?? key;
  if (typeof val === 'boolean') return `${label}: ${val ? 'ok' : 'down'}`;
  return `${label}: ${val}`;
}

export function OrchestratorHealthBar({ health }: OrchestratorHealthBarProps) {
  if (!health) {
    return (
      <div className="flex items-center gap-2 px-3 py-1.5 bg-neutral-900 border border-neutral-800 rounded-lg text-xs text-neutral-500 mb-3 shrink-0">
        <span className="w-1.5 h-1.5 rounded-full bg-neutral-600" />
        Orchestrator status unavailable
      </div>
    );
  }

  const cycleAge = health.last_cycle_seconds_ago;
  const cycleStale = cycleAge !== null && cycleAge > 120;

  const externalHealth = health.deploy_monitoring?.external_health ?? {};
  // Canonical order: github_api, db, gcp
  const orderedDeps = (['github_api', 'db', 'gcp'] as const).filter(k => k in externalHealth);

  return (
    <div className="flex items-center gap-4 px-3 py-1.5 bg-neutral-900 border border-neutral-800 rounded-lg text-xs mb-3 shrink-0">
      {/* Cycle age */}
      <div className="flex items-center gap-1.5" title="Time since last orchestrator cycle">
        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${cycleStale ? 'bg-amber-400 animate-pulse' : 'bg-emerald-500'}`} />
        <span className={cycleStale ? 'text-amber-400' : 'text-neutral-400'}>
          Cycle:{' '}
          {cycleAge === null
            ? '—'
            : cycleAge < 60
            ? `${Math.round(cycleAge)}s`
            : `${Math.round(cycleAge / 60)}m`}
          {cycleStale && ' ⚠'}
        </span>
      </div>

      <div className="w-px h-3 bg-neutral-800 shrink-0" />

      {/* Task counts */}
      <div className="flex items-center gap-3 text-neutral-400">
        <span>
          <span className="text-neutral-300 font-medium">{health.queued_tasks}</span> queued
        </span>
        <span>
          <span className="text-neutral-300 font-medium">{health.active_tasks}</span> active
        </span>
      </div>

      <div className="w-px h-3 bg-neutral-800 shrink-0" />

      {/* External dependency dots */}
      <div className="flex items-center gap-2">
        {orderedDeps.map(key => {
          const val = externalHealth[key] as boolean | string;
          return (
            <div
              key={key}
              className="flex items-center gap-1"
              title={statusDotTitle(key, val)}
            >
              <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${statusColor(val)}`} />
              <span className="text-neutral-500">{DEP_LABELS[key] ?? key}</span>
            </div>
          );
        })}
        {orderedDeps.length === 0 && (
          <span className="text-neutral-600">No deps</span>
        )}
      </div>
    </div>
  );
}
