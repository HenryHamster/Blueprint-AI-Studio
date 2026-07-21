import type { ResidentEngineHealth } from '../strategicStateApi';

interface ResidentEngineHealthChipProps {
  health: ResidentEngineHealth;
}

export function ResidentEngineHealthChip({ health }: ResidentEngineHealthChipProps) {
  const activeTurn = health.turns?.active ?? null;
  const successorCount = Number(Boolean(health.turns?.interactive_successor))
    + Number(Boolean(health.turns?.background_successor));
  const activeStatus = activeTurn?.status?.toUpperCase() ?? null;
  const unsettledOperations = health.operations?.unsettled_receipt_count ?? 0;
  const isStrategicOwner = health.engine_owner === 'strategic_resident';
  const isPaused = health.background_policy === 'paused';
  const unhealthy = health.health_status && !['healthy', 'ready', 'active'].includes(health.health_status.toLowerCase());
  const label = !health.resident_initialized
    ? 'Resident setup pending'
    : !isStrategicOwner
      ? 'Resident shadowing'
      : activeTurn
        ? activeStatus === 'QUEUED'
          ? 'Resident queued'
          : activeStatus === 'PROVISIONING'
            ? 'Resident finding a runner'
            : activeStatus === 'EVALUATING'
              ? 'Resident evaluating'
              : 'Resident working'
        : isPaused
          ? 'Background autonomy paused'
          : unhealthy
            ? `Resident ${health.health_status}`
            : 'Autonomy on · standing by';
  const tone = !health.resident_initialized || isPaused
    ? 'border-amber-800/70 bg-amber-950/35 text-amber-200'
    : unhealthy || unsettledOperations > 0
      ? 'border-rose-800/70 bg-rose-950/35 text-rose-200'
      : activeTurn
        ? 'border-indigo-700/70 bg-indigo-950/35 text-indigo-200'
        : 'border-emerald-800/70 bg-emerald-950/30 text-emerald-200';
  const soakDays = health.soak && health.soak.required_seconds > 0
    ? Math.min(health.soak.observed_seconds / 86_400, health.soak.required_seconds / 86_400)
    : null;
  const title = [
    health.engine_version ? `Engine ${health.engine_version}` : null,
    health.authority_generation != null ? `authority ${health.authority_generation}` : null,
    health.health_status ? `health ${health.health_status}` : null,
    health.blockers?.length ? `blockers: ${health.blockers.join(', ')}` : null,
  ].filter(Boolean).join(' · ');

  return (
    <div
      className={`inline-flex min-w-0 items-center gap-1.5 rounded-full border px-2 py-1 text-ui-sm ${tone}`}
      data-testid="resident-engine-health"
      role="status"
      title={title}
    >
      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${activeTurn ? 'animate-pulse bg-current' : 'bg-current'}`} aria-hidden="true" />
      <span className="font-medium">{label}</span>
      {successorCount > 0 && <span className="opacity-75">· {successorCount} queued</span>}
      {unsettledOperations > 0 && <span className="opacity-75">· {unsettledOperations} unsettled</span>}
      {soakDays != null && !health.legacy_retirement_ready && <span className="hidden opacity-70 sm:inline">· soak {soakDays.toFixed(1)}d</span>}
    </div>
  );
}
