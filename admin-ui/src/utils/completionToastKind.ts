const COMPLETION_ERROR_STATUSES = new Set([
  'error', 'failed', 'failure', 'aborted', 'timeout', 'timed_out',
]);
const COMPLETION_CANCELLED_STATUSES = new Set(['cancelled', 'canceled']);

export type CompletionToastKind = 'success' | 'error' | 'info';

/**
 * Maps a StrategicCycleCompletion.status string to the correct toast kind.
 * FAILED/error statuses → 'error', CANCELLED → 'info', otherwise → 'success'.
 */
export function resolveCompletionToastKind(status: string | undefined): CompletionToastKind {
  const s = status?.toLowerCase() ?? '';
  if (COMPLETION_ERROR_STATUSES.has(s)) return 'error';
  if (COMPLETION_CANCELLED_STATUSES.has(s)) return 'info';
  return 'success';
}

export function resolveCompletionToastMessage(kind: CompletionToastKind): string {
  if (kind === 'error') return 'Strategic cycle failed';
  if (kind === 'info') return 'Strategic cycle cancelled';
  return 'Strategic cycle complete';
}

const INTENTIONAL_RESIDENT_STOP_SOURCE = /(?:["']?source["']?)\s*(?:=|:)\s*["']?resident_operator_stop\b/i;

/**
 * Intentional operator stops remain part of the durable cycle lifecycle, but
 * are not exceptional enough to interrupt the operator with a sticky toast.
 * Fail closed: only a cancelled status plus the exact durable source marker is
 * suppressed; every unmarked cancellation and every failure remains visible.
 */
export function shouldSuppressCompletionToast(
  status: string | undefined,
  failureReason?: string,
  statusMessage?: string,
): boolean {
  const normalizedStatus = status?.toLowerCase();
  if (normalizedStatus !== 'cancelled' && normalizedStatus !== 'canceled') return false;
  return [failureReason, statusMessage].some(
    detail => typeof detail === 'string' && INTENTIONAL_RESIDENT_STOP_SOURCE.test(detail),
  );
}
