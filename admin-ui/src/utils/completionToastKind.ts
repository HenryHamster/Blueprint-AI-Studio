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
