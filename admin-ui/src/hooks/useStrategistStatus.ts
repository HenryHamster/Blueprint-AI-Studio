import { useMemo } from 'react';
import type { StrategicCycleProgress, StrategicCycleCompletion } from './useLogViewer';
import type { StrategicStateCycle } from '../strategicStateApi';

export type StrategistPhase = 'idle' | 'busy' | 'error' | 'done';
export type StrategistSource = 'cycle_progress' | 'active_cycle' | 'completion' | 'pending_steer' | 'steer_received' | 'idle';

export interface StrategistStatus {
  phase: StrategistPhase;
  label: string;
  detail?: string | null;
  activityKind?: 'autonomous' | 'operator' | null;
  source: StrategistSource;
}

export interface UseStrategistStatusInput {
  strategistActive?: boolean;
  cycleProgress?: StrategicCycleProgress | null;
  lastCycleCompletion?: StrategicCycleCompletion | null;
  activeCycle?: StrategicStateCycle | null;
  pendingRefreshAt?: string | null;
  lastFiredAt?: string | null;
}

const ERROR_STATUSES = new Set(['error', 'failed', 'failure', 'aborted', 'timeout', 'timed_out', 'cancelled', 'canceled']);

export type StrategicCycleTerminalDisposition = 'completed' | 'failed' | 'cancelled';

export function strategicCycleTerminalDisposition(
  status?: string | null,
): StrategicCycleTerminalDisposition | null {
  const normalized = status?.toLowerCase() ?? '';
  if (normalized === 'cancelled' || normalized === 'canceled' || normalized.includes('cancel')) {
    return 'cancelled';
  }
  if (
    normalized === 'failed'
    || normalized.includes('fail')
    || ['error', 'failure', 'aborted', 'timeout', 'timed_out'].includes(normalized)
  ) {
    return 'failed';
  }
  if (normalized === 'completed' || normalized === 'ok' || normalized === 'success' || normalized === 'succeeded') {
    return 'completed';
  }
  return null;
}

export function strategicCycleTerminalLabel(disposition: StrategicCycleTerminalDisposition): string {
  if (disposition === 'cancelled') return 'Cycle cancelled';
  if (disposition === 'failed') return 'Cycle failed';
  return 'Cycle complete';
}

export function useStrategistStatus({
  strategistActive,
  cycleProgress,
  lastCycleCompletion,
  activeCycle,
  pendingRefreshAt,
  lastFiredAt,
}: UseStrategistStatusInput): StrategistStatus {
  return useMemo((): StrategistStatus => {
    // Busy: strategist cycle actively running (WS event takes precedence over poll)
    if (strategistActive || activeCycle?.has_active_task) {
      const label = cycleProgress?.phase_label
        ? `Strategist: ${cycleProgress.phase_label}…`
        : 'Strategist working…';
      return {
        phase: 'busy',
        label,
        detail: cycleProgress?.steering_excerpt ?? null,
        activityKind: null,
        source: strategistActive ? 'cycle_progress' : 'active_cycle',
      };
    }

    // Pending steer: operator directive armed but cycle not yet started
    if (pendingRefreshAt) {
      return {
        phase: 'busy',
        label: 'Strategic cycle pending',
        detail: null,
        activityKind: 'operator',
        source: 'pending_steer',
      };
    }

    // Completion receipt: last cycle finished
    if (lastCycleCompletion) {
      const status = lastCycleCompletion.status?.toLowerCase() ?? '';
      const terminalDisposition = strategicCycleTerminalDisposition(status);
      const isError = Boolean(status) && (ERROR_STATUSES.has(status) || terminalDisposition === 'failed');
      const label = terminalDisposition
        ? strategicCycleTerminalLabel(terminalDisposition)
        : isError
          ? `Cycle ${status}`
          : 'Cycle complete';
      return {
        phase: isError || terminalDisposition === 'cancelled' ? 'error' : 'done',
        label,
        detail: lastCycleCompletion.failure_reason ?? lastCycleCompletion.status_message ?? null,
        activityKind: null,
        source: 'completion',
      };
    }

    // Steer received: cycle ran recently but no completion receipt in memory
    if (lastFiredAt) {
      return {
        phase: 'done',
        label: 'Steer picked up',
        detail: null,
        activityKind: null,
        source: 'steer_received',
      };
    }

    return { phase: 'idle', label: 'Strategist idle', detail: null, activityKind: null, source: 'idle' };
  }, [strategistActive, cycleProgress, lastCycleCompletion, activeCycle, pendingRefreshAt, lastFiredAt]);
}
