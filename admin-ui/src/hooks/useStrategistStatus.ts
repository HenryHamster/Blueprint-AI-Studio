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
  latestCycle?: StrategicStateCycle | null;
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

export function strategicCycleCompletionMatchesCycle(
  completion?: StrategicCycleCompletion | null,
  cycle?: Pick<StrategicStateCycle, 'id'> | null,
): boolean {
  return Boolean(completion?.cycle_id && cycle?.id && completion.cycle_id === cycle.id);
}

export function strategicCycleIsWorking({
  strategistActive = false,
  activeCycle,
  lastCycleCompletion,
}: Pick<UseStrategistStatusInput, 'strategistActive' | 'activeCycle' | 'lastCycleCompletion'>): boolean {
  return !strategicCycleCompletionMatchesCycle(lastCycleCompletion, activeCycle)
    && (strategistActive || Boolean(activeCycle?.has_active_task));
}

export interface StrategicCycleTerminalProjection {
  disposition: StrategicCycleTerminalDisposition | null;
  detail: string | null;
}

export function strategicCycleTerminalProjection({
  latestCycle,
  lastCycleCompletion,
}: Pick<UseStrategistStatusInput, 'latestCycle' | 'lastCycleCompletion'>): StrategicCycleTerminalProjection {
  if (!lastCycleCompletion) {
    return {
      disposition: strategicCycleTerminalDisposition(latestCycle?.terminal_disposition ?? latestCycle?.outcome),
      detail: latestCycle?.failure_reason ?? latestCycle?.summary_text ?? null,
    };
  }

  const durableLatestMatches = strategicCycleCompletionMatchesCycle(lastCycleCompletion, latestCycle);
  return {
    disposition: durableLatestMatches
      ? strategicCycleTerminalDisposition(latestCycle?.terminal_disposition ?? latestCycle?.outcome)
        ?? strategicCycleTerminalDisposition(lastCycleCompletion.status)
      : strategicCycleTerminalDisposition(lastCycleCompletion.status),
    detail: durableLatestMatches
      ? latestCycle?.failure_reason
        ?? latestCycle?.summary_text
        ?? lastCycleCompletion.failure_reason
        ?? lastCycleCompletion.status_message
        ?? null
      : lastCycleCompletion.failure_reason ?? lastCycleCompletion.status_message ?? null,
  };
}

export function useStrategistStatus({
  strategistActive,
  cycleProgress,
  lastCycleCompletion,
  activeCycle,
  latestCycle,
  pendingRefreshAt,
  lastFiredAt,
}: UseStrategistStatusInput): StrategistStatus {
  return useMemo((): StrategistStatus => {
    // Busy: strategist cycle actively running (WS event takes precedence over poll)
    if (strategicCycleIsWorking({ strategistActive, activeCycle, lastCycleCompletion })) {
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
      const terminalProjection = strategicCycleTerminalProjection({ latestCycle, lastCycleCompletion });
      const terminalDisposition = terminalProjection.disposition;
      const isError = Boolean(status) && (ERROR_STATUSES.has(status) || terminalDisposition === 'failed');
      const label = terminalDisposition
        ? strategicCycleTerminalLabel(terminalDisposition)
        : isError
          ? `Cycle ${status}`
          : 'Cycle complete';
      return {
        phase: isError || terminalDisposition === 'cancelled' ? 'error' : 'done',
        label,
        detail: terminalProjection.detail,
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
  }, [strategistActive, cycleProgress, lastCycleCompletion, activeCycle, latestCycle, pendingRefreshAt, lastFiredAt]);
}
