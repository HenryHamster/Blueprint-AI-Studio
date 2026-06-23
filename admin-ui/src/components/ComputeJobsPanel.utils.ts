import type { ComputeJob, ComputeRpcDiagnostic } from '../api';
import { parseTimestamp } from '../utils/time';
export { formatDurationSec as fmtDuration } from '../utils/time';

const ACTIVE_STATUSES: ReadonlySet<ComputeJob['status']> = new Set([
  'QUEUED',
  'STARTING',
  'RUNNING',
  'POLLING',
]);

export function isComputeJobActive(job: ComputeJob): boolean {
  return ACTIVE_STATUSES.has(job.status);
}

/** Human-readable terminal reason for a finished job, distinguishing the three
 *  kill paths (operator / budget / deadline) plus natural success/failure. */
export function computeJobTerminalLabel(job: ComputeJob): string {
  switch (job.outcome) {
    case 'SPEND_CAP_EXCEEDED':
      return 'Killed: budget cap';
    case 'TIMED_OUT':
      return 'Killed: deadline';
    case 'CANCELLED':
      return (job.terminal_reason || '').toLowerCase().includes('operator')
        ? 'Killed by operator'
        : 'Cancelled';
    case 'SUCCESS':
      return 'Succeeded';
    case 'FAILURE':
      return 'Failed';
    default:
      return job.status;
  }
}

export function fmtUsd(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return `$${v.toFixed(2)}`;
}

/** Remaining time until the wall-clock deadline, as a compact countdown. */
export function fmtCountdown(deadlineIso: string | null, nowMs: number): string {
  if (!deadlineIso) return '—';
  const t = parseTimestamp(deadlineIso).getTime();
  if (Number.isNaN(t)) return '—';
  const remaining = Math.round((t - nowMs) / 1000);
  if (remaining <= 0) return 'expired';
  if (remaining < 60) return `${remaining}s`;
  if (remaining < 3600) return `${Math.floor(remaining / 60)}m ${remaining % 60}s`;
  return `${Math.floor(remaining / 3600)}h ${Math.floor((remaining % 3600) / 60)}m`;
}

/** Backend RPC failure diagnostic for a job, reading the dedicated field and
 *  falling back to the legacy `polling_metadata.compute_rpc_diagnostic` shape so
 *  older API responses still surface the warning. Returns null when absent. */
export function computeJobRpcDiagnostic(job: ComputeJob): ComputeRpcDiagnostic | null {
  const direct = job.compute_rpc_diagnostic;
  if (direct && typeof direct === 'object') return direct;
  const legacy = (job.polling_metadata as Record<string, unknown> | undefined)?.[
    'compute_rpc_diagnostic'
  ];
  if (legacy && typeof legacy === 'object') return legacy as ComputeRpcDiagnostic;
  return null;
}

/** Spend used so far for a job — accrued while active, final once terminal. */
export function computeJobSpent(job: ComputeJob): number {
  if (isComputeJobActive(job)) return job.accrued_cost_usd ?? 0;
  return job.final_cost_usd ?? job.accrued_cost_usd ?? 0;
}

/** Actual wall-clock runtime in seconds for a job that has both timestamps.
 *  Returns null when either timestamp is absent or unparseable. */
export function computeJobRuntimeSec(job: ComputeJob): number | null {
  if (!job.started_at || !job.completed_at) return null;
  const start = parseTimestamp(job.started_at).getTime();
  const end = parseTimestamp(job.completed_at).getTime();
  if (Number.isNaN(start) || Number.isNaN(end) || end <= start) return null;
  return Math.round((end - start) / 1000);
}
