import { Cpu, LoaderCircle, MonitorCog, Server } from 'lucide-react';

export type StrategicInteractionMode = 'chat' | 'check_in' | 'monitor' | 'continuous';

export interface StrategicLocalRunnerStatus {
  status?: string | null;
  mode?: string | null;
  connected?: boolean | null;
  active?: boolean | null;
  label?: string | null;
  detail?: string | null;
  deviceCount?: number | null;
  onlineDevices?: number | null;
  totalDevices?: number | null;
  strategic_runner_count?: number | null;
  live_runner_count?: number | null;
  eligible_live_count?: number | null;
  stale_runner_count?: number | null;
  busy_runner_count?: number | null;
  agent_unavailable_count?: number | null;
  active_runner_id?: string | null;
  fallback_reason?: string | null;
}

export interface StrategicRuntimeWorkStatus {
  status?: string | null;
  active?: boolean | null;
  label?: string | null;
  detail?: string | null;
  runningCount?: number | null;
  activeCount?: number | null;
  queuedCount?: number | null;
  totalCount?: number | null;
  compute_jobs?: {
    counts?: {
      active?: number | null;
      terminal_recent?: number | null;
      total?: number | null;
      queued?: number | null;
      starting?: number | null;
      running?: number | null;
      polling?: number | null;
      failed_recent?: number | null;
      timed_out_recent?: number | null;
    } | null;
  } | null;
  held_approvals?: {
    count?: number | null;
  } | null;
  parked_wakes?: {
    counts?: {
      active?: number | null;
      problem?: number | null;
    } | null;
  } | null;
}

export interface StrategicSessionControlProps {
  currentMode?: StrategicInteractionMode;
  mode?: StrategicInteractionMode;
  saving?: boolean;
  localRunnerStatus?: StrategicLocalRunnerStatus | null;
  localRunner?: StrategicLocalRunnerStatus | null;
  runtimeWorkStatus?: StrategicRuntimeWorkStatus | null;
  runtimeWork?: StrategicRuntimeWorkStatus | null;
  onModeChange?: (mode: StrategicInteractionMode) => void;
  onOpenDevices?: () => void;
  onOpenCompute?: () => void;
  className?: string;
}

type Tone = 'neutral' | 'success' | 'warning' | 'danger' | 'info';

const MODE_OPTIONS: Array<{ value: StrategicInteractionMode; label: string }> = [
  { value: 'chat', label: 'Chat' },
  { value: 'check_in', label: 'Check-in' },
  { value: 'monitor', label: 'Monitor' },
  { value: 'continuous', label: 'Workday' },
];

const toneClasses: Record<Tone, { border: string; bg: string; icon: string; text: string; dot: string }> = {
  neutral: {
    border: 'border-neutral-800',
    bg: 'bg-neutral-950/60',
    icon: 'text-neutral-500',
    text: 'text-neutral-300',
    dot: 'bg-neutral-500',
  },
  success: {
    border: 'border-emerald-500/30',
    bg: 'bg-emerald-500/10',
    icon: 'text-emerald-400',
    text: 'text-emerald-300',
    dot: 'bg-emerald-400',
  },
  warning: {
    border: 'border-amber-500/30',
    bg: 'bg-amber-500/10',
    icon: 'text-amber-400',
    text: 'text-amber-300',
    dot: 'bg-amber-400',
  },
  danger: {
    border: 'border-red-500/30',
    bg: 'bg-red-500/10',
    icon: 'text-red-400',
    text: 'text-red-300',
    dot: 'bg-red-400',
  },
  info: {
    border: 'border-cyan-500/30',
    bg: 'bg-cyan-500/10',
    icon: 'text-cyan-400',
    text: 'text-cyan-300',
    dot: 'bg-cyan-400',
  },
};

function cleanText(value: string | null | undefined): string | null {
  const trimmed = value?.trim();
  return trimmed ? trimmed : null;
}

function normalizedStatus(value: string | null | undefined): string {
  return cleanText(value)?.toLowerCase().replace(/\s+/g, '_') ?? 'unknown';
}

function humanizeStatus(value: string): string {
  return value
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b\w/g, char => char.toUpperCase());
}

function firstCount(...values: Array<number | null | undefined>): number | null {
  for (const value of values) {
    if (typeof value === 'number' && Number.isFinite(value) && value >= 0) return value;
  }
  return null;
}

function pluralize(count: number, singular: string): string {
  return `${count} ${singular}${count === 1 ? '' : 's'}`;
}

function runnerStatusValue(status: StrategicLocalRunnerStatus): string {
  return normalizedStatus(status.status ?? status.mode);
}

function runnerTone(status: StrategicLocalRunnerStatus): Tone {
  const value = runnerStatusValue(status);
  if (value.includes('error') || value.includes('fail')) return 'danger';
  if (value.includes('start') || value.includes('sync') || value.includes('warn')) return 'warning';
  if (status.connected === true || status.active === true) return 'success';
  if ((firstCount(status.eligible_live_count) ?? 0) > 0) return 'success';
  if (['online', 'connected', 'ready', 'running', 'active', 'local', 'local_ready'].includes(value)) return 'success';
  if (value === 'pending_local') return 'warning';
  if (['offline', 'disconnected', 'stopped', 'disabled'].includes(value)) return 'neutral';
  return 'info';
}

function runnerLabel(status: StrategicLocalRunnerStatus): string {
  const explicit = cleanText(status.label);
  if (explicit) return explicit;
  if (status.connected === true || status.active === true) return 'Online';
  if (status.connected === false || status.active === false) return 'Offline';
  const value = runnerStatusValue(status);
  if (value === 'local') return 'Local';
  if (value === 'local_ready') return 'Ready';
  if (value === 'pending_local') return 'Waiting';
  if (value === 'cloud') return 'Cloud';
  return humanizeStatus(value);
}

function runnerDetail(status: StrategicLocalRunnerStatus): string | null {
  const explicit = cleanText(status.detail);
  if (explicit) return explicit;
  const online = firstCount(status.onlineDevices);
  const total = firstCount(status.totalDevices);
  const count = firstCount(status.deviceCount);
  if (online != null && total != null) return `${online}/${total} devices`;
  if (count != null) return `${count} device${count === 1 ? '' : 's'}`;
  const ready = firstCount(status.eligible_live_count);
  if (ready != null && ready > 0) return `${ready} ready`;
  const busy = firstCount(status.busy_runner_count);
  if (busy != null && busy > 0) return `${busy} busy`;
  const live = firstCount(status.live_runner_count);
  const enrolled = firstCount(status.strategic_runner_count);
  if (live != null && enrolled != null) return `${live}/${enrolled} live`;
  if (enrolled != null) return pluralize(enrolled, 'runner');
  return null;
}

function runtimeComputeCounts(status: StrategicRuntimeWorkStatus): NonNullable<StrategicRuntimeWorkStatus['compute_jobs']>['counts'] {
  return status.compute_jobs?.counts ?? null;
}

function runtimeHeldApprovalCount(status: StrategicRuntimeWorkStatus): number {
  return firstCount(status.held_approvals?.count) ?? 0;
}

function runtimeParkedWakeActiveCount(status: StrategicRuntimeWorkStatus): number {
  return firstCount(status.parked_wakes?.counts?.active) ?? 0;
}

function runtimeParkedWakeProblemCount(status: StrategicRuntimeWorkStatus): number {
  return firstCount(status.parked_wakes?.counts?.problem) ?? 0;
}

function runtimeRecentFailureCount(status: StrategicRuntimeWorkStatus): number {
  const counts = runtimeComputeCounts(status);
  return (firstCount(counts?.failed_recent) ?? 0) + (firstCount(counts?.timed_out_recent) ?? 0);
}

function runtimeTone(status: StrategicRuntimeWorkStatus): Tone {
  const value = normalizedStatus(status.status);
  if (runtimeRecentFailureCount(status) > 0) return 'danger';
  if (runtimeHeldApprovalCount(status) > 0 || runtimeParkedWakeProblemCount(status) > 0) return 'warning';
  if (value.includes('error') || value.includes('fail')) return 'danger';
  if (value.includes('block') || value.includes('queued') || value.includes('wait')) return 'warning';
  if (runtimeIsUseful(status)) return 'success';
  if (value === 'idle' || value === 'none' || value === 'unknown') return 'neutral';
  return 'info';
}

function runtimeActiveCount(status: StrategicRuntimeWorkStatus): number {
  const counts = runtimeComputeCounts(status);
  return firstCount(status.runningCount, status.activeCount, counts?.active)
    ?? (firstCount(counts?.running) ?? 0) + (firstCount(counts?.polling) ?? 0) + runtimeParkedWakeActiveCount(status);
}

function runtimeIsUseful(status: StrategicRuntimeWorkStatus): boolean {
  const value = normalizedStatus(status.status);
  return Boolean(
    status.active === true
      || runtimeActiveCount(status) > 0
      || (firstCount(status.queuedCount, runtimeComputeCounts(status)?.queued) ?? 0) > 0
      || runtimeHeldApprovalCount(status) > 0
      || runtimeParkedWakeProblemCount(status) > 0
      || runtimeRecentFailureCount(status) > 0
      || (firstCount(runtimeComputeCounts(status)?.terminal_recent) ?? 0) > 0
      || ['active', 'running', 'queued', 'blocked', 'error', 'failed', 'provisioning', 'evaluating'].includes(value),
  );
}

function runtimeLabel(status: StrategicRuntimeWorkStatus): string {
  const explicit = cleanText(status.label);
  if (explicit) return explicit;
  if (runtimeHeldApprovalCount(status) > 0 || runtimeParkedWakeProblemCount(status) > 0) return 'Needs check';
  if (runtimeRecentFailureCount(status) > 0) return 'Recent failure';
  const active = runtimeActiveCount(status);
  if (active > 0) return 'Active';
  const queued = firstCount(status.queuedCount, runtimeComputeCounts(status)?.queued) ?? 0;
  if (queued > 0) return 'Queued';
  if ((firstCount(runtimeComputeCounts(status)?.terminal_recent) ?? 0) > 0) return 'Recent result';
  return humanizeStatus(normalizedStatus(status.status));
}

function runtimeDetail(status: StrategicRuntimeWorkStatus): string | null {
  const explicit = cleanText(status.detail);
  if (explicit) return explicit;
  const active = runtimeActiveCount(status);
  const queued = firstCount(status.queuedCount, runtimeComputeCounts(status)?.queued) ?? 0;
  const parts = [
    active > 0 ? `${active} active` : null,
    queued > 0 ? `${queued} queued` : null,
    runtimeHeldApprovalCount(status) > 0 ? pluralize(runtimeHeldApprovalCount(status), 'approval') : null,
    runtimeParkedWakeProblemCount(status) > 0 ? pluralize(runtimeParkedWakeProblemCount(status), 'wake issue') : null,
    runtimeRecentFailureCount(status) > 0 ? pluralize(runtimeRecentFailureCount(status), 'recent failure') : null,
  ].filter(Boolean);
  if (parts.length > 0) return parts.join(' / ');
  const terminal = firstCount(runtimeComputeCounts(status)?.terminal_recent) ?? 0;
  if (terminal > 0) return pluralize(terminal, 'recent result');
  return null;
}

function StatusDot({ tone }: { tone: Tone }) {
  return <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${toneClasses[tone].dot}`} aria-hidden="true" />;
}

export function StrategicSessionControl({
  currentMode,
  mode,
  saving = false,
  localRunnerStatus = null,
  localRunner = null,
  runtimeWorkStatus = null,
  runtimeWork = null,
  onModeChange,
  onOpenDevices,
  onOpenCompute,
  className = '',
}: StrategicSessionControlProps) {
  const selectedMode = currentMode ?? mode ?? 'chat';
  const runnerStatus = localRunnerStatus ?? localRunner;
  const workStatus = runtimeWorkStatus ?? runtimeWork;

  const runner = runnerStatus
    ? {
        tone: runnerTone(runnerStatus),
        label: runnerLabel(runnerStatus),
        detail: runnerDetail(runnerStatus),
      }
    : null;

  const runtime = workStatus
    ? {
        tone: runtimeTone(workStatus),
        label: runtimeLabel(workStatus),
        detail: runtimeDetail(workStatus),
        useful: runtimeIsUseful(workStatus),
      }
    : null;

  return (
    <div
      className={`flex min-w-0 flex-col gap-2 text-neutral-100 sm:flex-row sm:items-center sm:justify-end ${className}`}
      data-testid="strategic-session-control"
    >
      <div
        className="grid min-w-0 grid-cols-2 gap-1 rounded-md border border-neutral-800 bg-neutral-950 p-1 sm:flex sm:w-auto"
        role="group"
        aria-label="Strategic interaction mode"
      >
        {MODE_OPTIONS.map(option => {
          const selected = option.value === selectedMode;
          return (
            <button
              key={option.value}
              type="button"
              disabled={saving || !onModeChange}
              aria-pressed={selected}
              aria-label={`Switch Strategic mode to ${option.label}`}
              title={`Strategic mode: ${option.label}`}
              onClick={() => {
                if (!selected) onModeChange?.(option.value);
              }}
              className={`min-h-11 min-w-0 rounded px-2.5 py-2 text-xs font-semibold transition-colors sm:min-h-9 sm:px-2 sm:py-1.5 ${
                selected
                  ? 'bg-neutral-800 text-neutral-100 ring-1 ring-neutral-700'
                  : 'text-neutral-400 hover:bg-neutral-900 hover:text-neutral-100'
              } disabled:cursor-not-allowed disabled:opacity-50`}
            >
              <span className="block truncate">{option.label}</span>
            </button>
          );
        })}
      </div>

      <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center sm:justify-end">
        {saving && (
          <span
            className="inline-flex min-h-11 min-w-0 items-center gap-2 rounded-md border border-cyan-500/25 bg-cyan-500/10 px-2.5 py-2 font-mono text-ui-sm font-semibold uppercase text-cyan-300 sm:min-h-9"
            role="status"
            aria-label="Saving Strategic session mode"
            title="Saving Strategic session mode"
          >
            <LoaderCircle className="h-3.5 w-3.5 shrink-0 animate-spin" aria-hidden="true" />
            Saving
          </span>
        )}

        {runner && (
          <button
            type="button"
            onClick={onOpenDevices}
            disabled={!onOpenDevices}
            className={`inline-flex min-h-11 w-full min-w-0 items-center gap-2 rounded-md border px-2.5 py-2 text-left transition-colors sm:min-h-9 sm:w-auto ${
              toneClasses[runner.tone].border
            } ${toneClasses[runner.tone].bg} hover:border-neutral-500 disabled:cursor-not-allowed disabled:opacity-60`}
            aria-label={`Open devices: runner ${runner.label}${runner.detail ? `, ${runner.detail}` : ''}`}
            title="Open devices"
          >
            <Server className={`h-4 w-4 shrink-0 ${toneClasses[runner.tone].icon}`} aria-hidden="true" />
            <span className="min-w-0 flex-1">
              <span className="flex min-w-0 items-center gap-1.5">
                <StatusDot tone={runner.tone} />
                <span className={`truncate text-xs font-semibold ${toneClasses[runner.tone].text}`}>{runner.label}</span>
              </span>
              <span className="block truncate font-mono text-ui-sm uppercase text-neutral-500">Runner</span>
            </span>
            {runner.detail && (
              <span className="hidden shrink-0 rounded border border-neutral-800 bg-neutral-950/60 px-1.5 py-0.5 font-mono text-ui-sm text-neutral-400 md:inline">
                {runner.detail}
              </span>
            )}
          </button>
        )}

        {runtime && runtime.useful && onOpenCompute ? (
          <button
            type="button"
            onClick={onOpenCompute}
            className={`inline-flex min-h-11 w-full min-w-0 items-center gap-2 rounded-md border px-2.5 py-2 text-left transition-colors hover:border-neutral-500 sm:min-h-9 sm:w-auto ${
              toneClasses[runtime.tone].border
            } ${toneClasses[runtime.tone].bg}`}
            aria-label={`Open compute: runtime work ${runtime.label}${runtime.detail ? `, ${runtime.detail}` : ''}`}
            title="Open compute"
          >
            <Cpu className={`h-4 w-4 shrink-0 ${toneClasses[runtime.tone].icon}`} aria-hidden="true" />
            <span className="min-w-0 flex-1">
              <span className="flex min-w-0 items-center gap-1.5">
                <StatusDot tone={runtime.tone} />
                <span className={`truncate text-xs font-semibold ${toneClasses[runtime.tone].text}`}>{runtime.label}</span>
              </span>
              <span className="block truncate font-mono text-ui-sm uppercase text-neutral-500">Compute</span>
            </span>
            {runtime.detail && (
              <span className="hidden shrink-0 rounded border border-neutral-800 bg-neutral-950/60 px-1.5 py-0.5 font-mono text-ui-sm text-neutral-400 md:inline">
                {runtime.detail}
              </span>
            )}
          </button>
        ) : runtime ? (
          <span
            className={`inline-flex min-h-11 w-full min-w-0 items-center gap-2 rounded-md border px-2.5 py-2 text-left sm:min-h-9 sm:w-auto ${
              toneClasses[runtime.tone].border
            } ${toneClasses[runtime.tone].bg}`}
            aria-label={`Runtime work ${runtime.label}${runtime.detail ? `, ${runtime.detail}` : ''}`}
            title="Runtime work"
          >
            <MonitorCog className={`h-4 w-4 shrink-0 ${toneClasses[runtime.tone].icon}`} aria-hidden="true" />
            <span className="min-w-0 flex-1">
              <span className="flex min-w-0 items-center gap-1.5">
                <StatusDot tone={runtime.tone} />
                <span className={`truncate text-xs font-semibold ${toneClasses[runtime.tone].text}`}>{runtime.label}</span>
              </span>
              <span className="block truncate font-mono text-ui-sm uppercase text-neutral-500">Compute</span>
            </span>
          </span>
        ) : null}
      </div>
    </div>
  );
}

export default StrategicSessionControl;
