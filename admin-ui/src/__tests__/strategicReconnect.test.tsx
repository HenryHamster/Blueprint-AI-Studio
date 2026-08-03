import { act, renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { useLogViewer } from '../hooks/useLogViewer';
import type { StrategicStateCycle, StrategicStateResponse } from '../strategicStateApi';

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api');
  return {
    ...actual,
    authHeaders: vi.fn().mockResolvedValue({ Authorization: 'Bearer test' }),
    API_BASE_URL: '',
    fetchTask: vi.fn(),
  };
});

const makeCycle = (overrides: Partial<StrategicStateCycle> = {}): StrategicStateCycle => ({
  id: 'cycle-reconnect',
  outcome: 'phase2_running',
  has_active_task: true,
  stale: false,
  phase1_task_id: 'phase-1',
  phase2_task_id: 'phase-2',
  phase1_started_at: null,
  phase1_completed_at: null,
  phase2_started_at: null,
  phase2_completed_at: null,
  summary_text: null,
  failure_reason: null,
  directions_created: 0,
  directions_retired: 0,
  tasks_spawned: 0,
  vision_updated: false,
  problem_model_changed: false,
  running_update_changed: false,
  ...overrides,
});

const makeState = (
  active_cycle: StrategicStateCycle | null,
  latest_cycle: StrategicStateCycle | null,
): StrategicStateResponse => ({
  project_id: 'project-reconnect',
  strategic: {
    active_cycle,
    active_task: null,
    active_phase: null,
    latest_cycle,
    refresh: { pending: null },
  },
} as StrategicStateResponse);

describe('Strategic reconnect projection', () => {
  it('restores_dropped_terminal_receipt_from_latest_cycle', () => {
    const strategicReadModelRef = {
      current: makeState(makeCycle({ phase2_task_id: null, outcome: 'phase1_running' }), null),
    };
    const { result } = renderHook(() => useLogViewer({
      fetchTasksRef: { current: vi.fn() },
      setDeployState: vi.fn(),
      selectedProjectId: null,
      realtimeWsEnabled: false,
      strategicReadModelRef,
    }));

    act(() => {
      result.current.projectStrategicState(strategicReadModelRef.current);
    });
    expect(result.current.strategistActive).toBe(true);

    strategicReadModelRef.current = makeState(makeCycle({
      phase2_task_id: 'phase-2',
      outcome: 'phase2_running',
    }), null);
    act(() => { result.current.projectStrategicState(strategicReadModelRef.current); });
    expect(result.current.cycleProgress).toMatchObject({
      task_id: 'phase-2', phase_number: 2, phase_label: 'Strategic review', phase_state: 'phase2_running',
    });

    strategicReadModelRef.current = makeState(null, makeCycle({
      outcome: 'completed',
      has_active_task: false,
      summary_text: 'Created one durable direction.',
      directions_created: 1,
      tasks_spawned: 2,
      problem_model_changed: true,
    }));
    act(() => {
      result.current.projectStrategicState(strategicReadModelRef.current);
    });

    expect(result.current.strategistActive).toBe(false);
    expect(result.current.cycleProgress).toBeNull();
    expect(result.current.lastCycleCompletion).toMatchObject({
      cycle_id: 'cycle-reconnect',
      status: 'completed',
      created_count: 1,
      tasks_spawned: 2,
      problem_model_changed: true,
      status_message: 'Created one durable direction.',
    });
  });
});
