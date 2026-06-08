import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useRef } from 'react';
import { useLogViewer } from './useLogViewer';

vi.mock('../api', () => ({
  authHeaders: vi.fn().mockResolvedValue({ Authorization: 'Bearer ***' }),
  API_BASE_URL: '',
  fetchTask: vi.fn(),
}));

// Minimal WebSocket stub that captures the instance so tests can drive
// onopen / onmessage manually.
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
  constructor(public url: string) {
    MockWebSocket.instances.push(this);
  }
  emit(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent);
  }
}

let fetchSpy: ReturnType<typeof vi.fn>;

function setup() {
  return renderHook(() => {
    const fetchTasksRef = useRef<() => void>(vi.fn());
    const fetchDirectionsRef = useRef<() => void>(vi.fn());
    const setDeployState = vi.fn();
    const lv = useLogViewer({ fetchTasksRef, setDeployState, fetchDirectionsRef });
    return { lv, fetchDirectionsRef };
  });
}

describe('useLogViewer strategist events', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    MockWebSocket.instances = [];
    fetchSpy = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ticket: 't' }) });
    vi.stubGlobal('fetch', fetchSpy);
    vi.stubGlobal('WebSocket', MockWebSocket as unknown as typeof WebSocket);
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  async function connect(result: ReturnType<typeof setup>['result']) {
    // trackTask opens the WS for the project without needing a log drawer.
    // connectWs awaits a ticket fetch (two microtask points) before
    // constructing the WebSocket — flush microtasks, then fire onopen.
    await act(async () => {
      result.current.lv.trackTask({ id: 'task-1', project_id: 'proj1', title: 'T' } as never);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(MockWebSocket.instances.length).toBe(1);
    const ws = MockWebSocket.instances[0];
    act(() => { ws.onopen?.({}); });
    return ws;
  }

  it('debounces a directions refetch on strategic_updated', async () => {
    const { result } = setup();
    const ws = await connect(result);
    const fetchDirections = result.current.fetchDirectionsRef.current as ReturnType<typeof vi.fn>;
    fetchDirections.mockClear();

    act(() => { ws.emit({ event: 'strategic_updated', data: { kind: 'directions' } }); });
    expect(fetchDirections).not.toHaveBeenCalled(); // debounced
    act(() => { vi.advanceTimersByTime(500); });
    expect(fetchDirections).toHaveBeenCalledTimes(1);
  });

  it('toggles strategistActive on cycle start/completed and refetches on complete', async () => {
    const { result } = setup();
    const ws = await connect(result);
    const fetchDirections = result.current.fetchDirectionsRef.current as ReturnType<typeof vi.fn>;
    fetchDirections.mockClear();

    act(() => { ws.emit({ event: 'strategic_cycle_started', data: { task_id: 'task-1', phase: 'strategic_review' } }); });
    expect(result.current.lv.strategistActive).toBe(true);

    act(() => { ws.emit({ event: 'strategic_cycle_completed', data: { task_id: 'task-1', phase: 'strategic_review' } }); });
    expect(result.current.lv.strategistActive).toBe(false);
    act(() => { vi.advanceTimersByTime(500); });
    expect(fetchDirections).toHaveBeenCalledTimes(1);
  });

  it('resets strategistActive to false on WS (re)connect', async () => {
    const { result } = setup();
    const ws = await connect(result);

    act(() => { ws.emit({ event: 'strategic_cycle_started', data: { task_id: 'task-1', phase: 'strategic_review' } }); });
    expect(result.current.lv.strategistActive).toBe(true);

    // Simulate a reconnect: a dropped strategic_cycle_completed would otherwise
    // leave the indicator stuck on. onopen must clear it as a safety net.
    act(() => { ws.onopen?.({}); });
    expect(result.current.lv.strategistActive).toBe(false);
  });

  it('resets strategistActive to false when the selected project changes', async () => {
    const { result, rerender } = renderHook(
      ({ pid }: { pid: string | null }) => {
        const fetchTasksRef = useRef<() => void>(vi.fn());
        const fetchDirectionsRef = useRef<() => void>(vi.fn());
        const setDeployState = vi.fn();
        const lv = useLogViewer({ fetchTasksRef, setDeployState, fetchDirectionsRef, selectedProjectId: pid });
        return { lv, fetchDirectionsRef };
      },
      { initialProps: { pid: 'projA' as string | null } },
    );

    await act(async () => {
      result.current.lv.trackTask({ id: 'task-1', project_id: 'projA', title: 'T' } as never);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const ws = MockWebSocket.instances[0];
    act(() => { ws.onopen?.({}); });
    act(() => { ws.emit({ event: 'strategic_cycle_started', data: { task_id: 'task-1', phase: 'strategic_review' } }); });
    expect(result.current.lv.strategistActive).toBe(true);

    // Switching projects must clear the hook-global flag so project A's stale
    // "working" state never renders on project B's directions panel.
    act(() => { rerender({ pid: 'projB' }); });
    expect(result.current.lv.strategistActive).toBe(false);
  });

  it('ignores unrelated events', async () => {
    const { result } = setup();
    const ws = await connect(result);
    const fetchDirections = result.current.fetchDirectionsRef.current as ReturnType<typeof vi.fn>;
    fetchDirections.mockClear();

    act(() => { ws.emit({ event: 'pong' }); });
    act(() => { vi.advanceTimersByTime(500); });
    expect(fetchDirections).not.toHaveBeenCalled();
    expect(result.current.lv.strategistActive).toBe(false);
  });

  it('test_tracks_strategic_cycle_progress_and_completion_outcome', async () => {
    const { result } = setup();
    const ws = await connect(result);

    // Emit a cycle-started event with the enriched payload the backend now sends.
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
          steering_excerpt: 'Focus on improving test coverage',
        },
      });
    });

    // strategistActive must be set and cycleProgress must capture phase details.
    expect(result.current.lv.strategistActive).toBe(true);
    expect(result.current.lv.cycleProgress).toMatchObject({
      task_id: 'task-1',
      cycle_id: 'cycle-42',
      phase_number: 2,
      phase_label: 'Strategic review',
      phase_state: 'phase2_running',
      steering_origin: 'operator',
      steering_excerpt: 'Focus on improving test coverage',
    });
    // No completion yet.
    expect(result.current.lv.lastCycleCompletion).toBeNull();

    // Emit completion with an outcome summary.
    act(() => {
      ws.emit({
        event: 'strategic_cycle_completed',
        data: {
          task_id: 'task-1',
          cycle_id: 'cycle-42',
          phase: 2,
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
          },
        },
      });
    });

    // After completion: active flag cleared, progress cleared, outcome captured.
    expect(result.current.lv.strategistActive).toBe(false);
    expect(result.current.lv.cycleProgress).toBeNull();
    expect(result.current.lv.lastCycleCompletion).toMatchObject({
      status: 'completed',
      created_count: 2,
      retired_count: 1,
      tasks_spawned: 3,
      directions_changed: true,
      running_update_changed: true,
    });

    // Directions refetch must have been debounced and fired.
    const fetchDirections = result.current.fetchDirectionsRef.current as ReturnType<typeof vi.fn>;
    act(() => { vi.advanceTimersByTime(500); });
    expect(fetchDirections).toHaveBeenCalledTimes(1);
  });
});
