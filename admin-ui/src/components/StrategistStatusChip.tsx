import type { StrategistStatus } from '../hooks/useStrategistStatus';

interface StrategistStatusChipProps {
  status: StrategistStatus;
  className?: string;
}

function toneClasses(phase: StrategistStatus['phase']): string {
  switch (phase) {
    case 'busy':
      return 'border-indigo-700/70 bg-indigo-950/35 text-indigo-200';
    case 'error':
      return 'border-rose-800/70 bg-rose-950/35 text-rose-200';
    case 'done':
      return 'border-emerald-800/70 bg-emerald-950/30 text-emerald-200';
    default:
      return 'border-neutral-700/50 bg-neutral-900/40 text-neutral-400';
  }
}

export function StrategistStatusChip({ status, className = '' }: StrategistStatusChipProps) {
  const { phase, label, detail } = status;
  const tone = toneClasses(phase);
  const isPulsing = phase === 'busy';

  return (
    <div
      className={`inline-flex min-w-0 items-center gap-1.5 rounded-full border px-2 py-1 text-ui-sm ${tone} ${className}`}
      data-testid="strategist-status-chip"
      role="status"
      aria-live="polite"
      title={detail ?? label}
    >
      <span
        className={`h-1.5 w-1.5 shrink-0 rounded-full bg-current ${isPulsing ? 'animate-pulse' : ''}`}
        aria-hidden="true"
      />
      <span className="min-w-0 truncate font-medium">{label}</span>
      {detail && (
        <span className="hidden truncate opacity-75 sm:inline italic">
          &ldquo;{detail.slice(0, 60)}{detail.length > 60 ? '…' : ''}&rdquo;
        </span>
      )}
    </div>
  );
}
