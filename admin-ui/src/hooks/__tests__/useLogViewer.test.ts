/**
 * Reconnect-truth regressions for useLogViewer.
 *
 * Two scenarios must hold simultaneously:
 *   1. Active-cycle preservation — a WS reconnect during a durably active
 *      strategic cycle must leave strategistActive = true so the UI does not
 *      incorrectly show the strategist as idle.
 *   2. Dropped-completion recovery — a WS reconnect after a
 *      strategic_cycle_completed event was lost while disconnected must clear
 *      stale activity (strategistActive = false).
 *
 * The durable authority is the Strategic read-model ref passed in as
 * `strategicReadModelRef`; the hook projects its current cycle at onopen time.
 *
 * This file also retains the full canonical strategic_cycle_completed-envelope
 * test and verifies lifecycle-before-refetch ordering.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useRef } from 'react';
import { useLogViewer } from '../useLogViewer';
import type { StrategicStateCycle, StrategicStateResponse } from '../../strategicStateApi';

vi.mock('../../api', () => ({
  authHeaders: vi.fn().mockResolvedValue({ Authorization: 'Bearer ***' }),
  apiErrorMessage: vi.fn((_payload: unknown, fallback: string) => fallback),
  API_BASE_URL: '',
  fetchTask: vi.fn(async (id: string) => ({ id, project_id: 'projA', status: 'IN_PROGRESS', title: 'T' })),
}));

class MockWebSocket {
  static OPEN = 1;
  static CONNECTING = 0;
  static CLOSED = 3;
  static instances: MockWebSocket[] = [];
  readyState = MockWebSocket.OPEN;
  onopen: ((e: unknown) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: unknown) => void) | null = null;
  onclose: ((e: { code: number; reason: string }) => void) | null = null;
  send = vi.fn();
  close = vi.fn();
  url: string;
  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }
  emit(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent);
  }
}

function setup(options: {
  selectedProjectId?: string | null;
  realtimeWsEnabled?: boolean;
  strategicReadModelRef?: React.MutableRefObject<StrategicStateResponse | null>;
} = {}) {
  return renderHook(() => {
    const fetchTasksRef = useRef<() => void>(vi.fn());
    const fetchDirectionsRef = useRef<() => void>(vi.fn());
    const refreshStrategicReadModelRef = useRef<() => void>(vi.fn());
    const refreshMessagesRef = useRef<() => void>(vi.fn());
    const fetchAlertsRef = useRef<() => void>(vi.fn());
    const refreshFleetRunnersRef = useRef<() => void>(vi.fn());
    const setDeployState = vi.fn();
    const lv = useLogViewer({
      fetchTasksRef,
      setDeployState,
      fetchDirectionsRef,
      refreshStrategicReadModelRef,
      refreshMessagesRef,
      fetchAlertsRef,
      refreshFleetRunnersRef,
      selectedProjectId: options.selectedProjectId,
      realtimeWsEnabled: options.realtimeWsEnabled,
      strategicReadModelRef: options.strategicReadModelRef,
    });
    return { lv, fetchTasksRef, fetchDirectionsRef, refreshStrategicReadModelRef, refreshMessagesRef };
  });
}

function cycle(overrides: Partial<StrategicStateCycle> = {}): StrategicStateCycle {
  return {
    id: 'cycle-1',
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
  };
}

function strategicState(activeCycle: StrategicStateCycle | null, latestCycle: StrategicStateCycle | null): StrategicStateResponse {
  return {
    project_id: 'proj1',
    strategic: {
      active_cycle: activeCycle,
      active_task: null,
      active_phase: null,
      latest_cycle: latestCycle,
      refresh: { pending: null },
    },
  } as StrategicStateResponse;
}

async function connect(result: ReturnType<typeof setup>['result']) {
  await act(async () => {
    result.current.lv.trackTask({ id: 'task-1', project_id: 'proj1', status: 'IN_PROGRESS', title: 'T' } as never);
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(MockWebSocket.instances.length).toBe(1);
  const ws = MockWebSocket.instances[0];
  act(() => { ws.onopen?.({}); });
  return ws;
}

describe('useLogViewer reconnect truth', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    MockWebSocket.instances = [];
    const fetchSpy = vi.fn((url: RequestInfo | URL) => {
      const path = String(url);
      if (path.includes('/ws-ticket')) {
        return Promise.resolve({ ok: true, json: async () => ({ ticket: 't' }) });
      }
      if (path.includes('/failing-task-types')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            gated_types: [],
            global_failure_threshold: 0.6,
            anomaly_tightened: false,
            window_minutes: 15,
            generated_at: '2026-08-03T00:00:00Z',
          }),
        });
      }
      return Promise.resolve({ ok: true, json: async () => ({}) });
    });
    vi.stubGlobal('fetch', fetchSpy);
    vi.stubGlobal('WebSocket', MockWebSocket as unknown as typeof WebSocket);
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  // ── Scenario 1: active-cycle preservation ─────────────────────────────────
  it('preserves strategistActive on reconnect when cycle is durably active', async () => {
    // The durable read-model reports the cycle is still running.
    const strategicReadModelRef = { current: strategicState(cycle(), null) };
    const { result } = setup({ strategicReadModelRef });
    const ws = await connect(result);

    act(() => {
      ws.emit({ event: 'strategic_cycle_started', data: { task_id: 'task-1', phase: 'strategic_review' } });
    });
    expect(result.current.lv.strategistActive).toBe(true);

    // Reconnect while the cycle is still durably active — the indicator must
    // remain true so the UI does not incorrectly show the strategist as idle.
    act(() => { ws.onopen?.({}); });
    expect(result.current.lv.strategistActive).toBe(true);
  });

  // ── Scenario 2: dropped-completion recovery ───────────────────────────────
  it('clears strategistActive on reconnect when durable authority reports no active cycle', async () => {
    // The durable read-model reports no active cycle (cycle has finished).
    const strategicReadModelRef = {
      current: strategicState(null, cycle({ outcome: 'failed_phase2', has_active_task: false, summary_text: 'Strategic review failed' })),
    };
    const { result } = setup({ strategicReadModelRef });
    const ws = await connect(result);

    act(() => {
      ws.emit({ event: 'strategic_cycle_started', data: { task_id: 'task-1', phase: 'strategic_review' } });
    });
    expect(result.current.lv.strategistActive).toBe(true);

    // The strategic_cycle_completed was dropped while disconnected.
    // Reconnect must detect the mismatch via the durable ref and clear stale
    // activity so the operator does not see a stuck "working" indicator.
    act(() => { ws.onopen?.({}); });
    expect(result.current.lv.strategistActive).toBe(false);
  });

  it('restores a durable cancellation receipt after a dropped completion broadcast', async () => {
    const strategicReadModelRef = {
      current: strategicState(cycle(), null),
    };
    const { result } = setup({ strategicReadModelRef });
    const ws = await connect(result);

    act(() => {
      ws.emit({
        event: 'strategic_cycle_started',
        data: { task_id: 'task-1', cycle_id: 'cycle-cancelled', phase: 'strategic_review' },
      });
    });

    strategicReadModelRef.current = strategicState(null, cycle({
      id: 'cycle-cancelled',
      outcome: 'failed_phase1',
      has_active_task: false,
      terminal_disposition: 'cancelled',
      summary_text: 'Strategic review cancelled',
      failure_reason: 'Cancelled by operator',
    }));
    act(() => { ws.onopen?.({}); });

    expect(result.current.lv.lastCycleCompletion).toMatchObject({
      cycle_id: 'cycle-cancelled',
      status: 'CANCELLED',
      status_message: 'Strategic review cancelled',
      failure_reason: 'Cancelled by operator',
    });
  });

  // ── Backward compat: no durable ref behaves as before ─────────────────────
  it('resets strategistActive to false on reconnect when no durable ref is provided', async () => {
    // Without a durable authority ref the hook applies the conservative safety-net
    // reset (same as the original behavior).
    const { result } = setup();
    const ws = await connect(result);

    act(() => {
      ws.emit({ event: 'strategic_cycle_started', data: { task_id: 'task-1', phase: 'strategic_review' } });
    });
    expect(result.current.lv.strategistActive).toBe(true);

    act(() => { ws.onopen?.({}); });
    expect(result.current.lv.strategistActive).toBe(false);
  });

  // ── Lifecycle-before-refetch ordering ─────────────────────────────────────
  it('clears lifecycle state before the directions refetch fires on completion', async () => {
    const { result } = setup();
    const ws = await connect(result);
    const fetchDirections = result.current.fetchDirectionsRef.current as ReturnType<typeof vi.fn>;
    fetchDirections.mockClear();

    act(() => {
      ws.emit({ event: 'strategic_cycle_started', data: { task_id: 'task-1', phase: 'strategic_review' } });
    });
    expect(result.current.lv.strategistActive).toBe(true);

    act(() => {
      ws.emit({
        event: 'strategic_cycle_completed',
        data: {
          schema_version: 1,
          project_id: 'proj1',
          resource: 'strategic_cycle',
          action: 'completed',
          refetch: { surfaces: ['strategic_session', 'directions'] },
          task_id: 'task-1',
          cycle_id: 'cycle-1',
          completion: { status: 'completed', created_count: 1, tasks_spawned: 1 },
        },
      });
    });

    // Lifecycle cleared synchronously (before debounce fires).
    expect(result.current.lv.strategistActive).toBe(false);
    expect(result.current.lv.cycleProgress).toBeNull();

    // Directions refetch is debounced and fires after 500ms.
    expect(fetchDirections).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(500); });
    expect(fetchDirections).toHaveBeenCalledTimes(1);
  });

  // ── Full canonical strategic_cycle_completed envelope ─────────────────────
  it('captures full canonical v1 completion envelope: clears active state and records receipt', async () => {
    const { result } = setup();
    const ws = await connect(result);

    act(() => {
      ws.emit({
        event: 'strategic_cycle_started',
        data: {
          task_id: 'task-1',
          cycle_id: 'cycle-42',
          phase: 2,
          phase_number: 2,
          phase_label: 'Strategic review',
          phase_state: 'phase2_running',
          steering_origin: 'operator',
          steering_excerpt: 'Improve reconnect coverage',
        },
      });
    });

    expect(result.current.lv.strategistActive).toBe(true);
    expect(result.current.lv.cycleProgress).toMatchObject({
      task_id: 'task-1',
      cycle_id: 'cycle-42',
      phase_number: 2,
      phase_label: 'Strategic review',
      phase_state: 'phase2_running',
      steering_origin: 'operator',
      steering_excerpt: 'Improve reconnect coverage',
    });
    expect(result.current.lv.lastCycleCompletion).toBeNull();

    act(() => {
      ws.emit({
        event: 'strategic_cycle_completed',
        data: {
          schema_version: 1,
          project_id: 'proj1',
          resource: 'strategic_cycle',
          action: 'completed',
          refetch: { surfaces: ['strategic_session', 'directions'] },
          task_id: 'task-1',
          cycle_id: 'cycle-42',
          phase: 2,
          phase_number: 2,
          phase_label: 'Strategic review',
          phase_state: 'cycle_done',
          completion: {
            status: 'completed',
            created_count: 2,
            retired_count: 1,
            staged_count: 0,
            updated_count: 1,
            tasks_spawned: 3,
            directions_changed: true,
            problem_model_changed: false,
            running_update_changed: true,
            strategic_judgment_challenge: {
              mode: 'ok',
              verdict: 'warned',
              warned_count: 1,
              blocked_count: 0,
            },
          },
          strategic: {
            kind: 'cycle',
            task_id: 'task-1',
            phase: 2,
            cycle_id: 'cycle-42',
          },
        },
      });
    });

    // Lifecycle cleared synchronously.
    expect(result.current.lv.strategistActive).toBe(false);
    expect(result.current.lv.cycleProgress).toBeNull();

    // Completion receipt captured with all fields from the envelope.
    expect(result.current.lv.lastCycleCompletion).toMatchObject({
      status: 'completed',
      created_count: 2,
      retired_count: 1,
      tasks_spawned: 3,
      directions_changed: true,
      running_update_changed: true,
      strategic_judgment_challenge: {
        mode: 'ok',
        verdict: 'warned',
        warned_count: 1,
        blocked_count: 0,
      },
    });

    // Directions refetch fires after debounce (lifecycle-before-refetch ordering).
    const fetchDirections = result.current.fetchDirectionsRef.current as ReturnType<typeof vi.fn>;
    act(() => { vi.advanceTimersByTime(500); });
    expect(fetchDirections).toHaveBeenCalledTimes(1);
  });

  it('ignores a late prior-cycle completion without losing newer activity or receipt', async () => {
    const { result } = setup();
    const ws = await connect(result);

    act(() => {
      ws.emit({
        event: 'strategic_cycle_started',
        data: { task_id: 'task-a', cycle_id: 'cycle-a', phase_number: 1 },
      });
      ws.emit({
        event: 'strategic_cycle_completed',
        data: {
          cycle_id: 'cycle-a',
          completion: { status: 'failed', failure_reason: 'old cycle' },
        },
      });
      ws.emit({
        event: 'strategic_cycle_started',
        data: {
          task_id: 'task-b',
          cycle_id: 'cycle-b',
          phase_number: 2,
          phase_label: 'Strategic review',
        },
      });
    });

    expect(result.current.lv.strategistActive).toBe(true);
    expect(result.current.lv.cycleProgress).toMatchObject({
      task_id: 'task-b',
      cycle_id: 'cycle-b',
      phase_number: 2,
    });
    expect(result.current.lv.lastCycleCompletion).toBeNull();

    act(() => {
      ws.emit({
        event: 'strategic_cycle_completed',
        data: {
          cycle_id: 'cycle-a',
          completion: { status: 'failed', failure_reason: 'late old cycle' },
        },
      });
    });

    expect(result.current.lv.strategistActive).toBe(true);
    expect(result.current.lv.cycleProgress).toMatchObject({ task_id: 'task-b', cycle_id: 'cycle-b' });
    expect(result.current.lv.lastCycleCompletion).toBeNull();

    act(() => {
      ws.emit({
        event: 'strategic_cycle_completed',
        data: {
          cycle_id: 'cycle-b',
          completion: { status: 'completed', status_message: 'new cycle' },
        },
      });
    });
    expect(result.current.lv.lastCycleCompletion).toMatchObject({
      cycle_id: 'cycle-b',
      status: 'completed',
      status_message: 'new cycle',
    });

    act(() => {
      ws.emit({
        event: 'strategic_cycle_completed',
        data: {
          cycle_id: 'cycle-a',
          completion: { status: 'failed', failure_reason: 'late old cycle again' },
        },
      });
    });
    expect(result.current.lv.lastCycleCompletion).toMatchObject({
      cycle_id: 'cycle-b',
      status: 'completed',
      status_message: 'new cycle',
    });
  });

  // ── Active cycle preserved: cycleProgress also retained ───────────────────
  it('retains cycleProgress on reconnect when cycle is durably active', async () => {
    const strategicReadModelRef = { current: strategicState(cycle({ id: 'cycle-7' }), null) };
    const { result } = setup({ strategicReadModelRef });
    const ws = await connect(result);

    act(() => {
      ws.emit({
        event: 'strategic_cycle_started',
        data: {
          task_id: 'task-2',
          cycle_id: 'cycle-7',
          phase_number: 1,
          phase_label: 'Research',
          phase_state: 'phase1_running',
        },
      });
    });

    expect(result.current.lv.cycleProgress).toMatchObject({ task_id: 'task-2', cycle_id: 'cycle-7' });

    // Reconnect with durable authority confirming the cycle is still live.
    act(() => { ws.onopen?.({}); });

    // Activity remains live, while durable phase identity wins over the stale
    // phase-one event snapshot.
    expect(result.current.lv.strategistActive).toBe(true);
    expect(result.current.lv.cycleProgress).toMatchObject({
      task_id: 'phase-2',
      cycle_id: 'cycle-7',
      phase_number: 2,
      phase_label: 'Strategic review',
      phase_state: 'phase2_running',
    });
  });
});
