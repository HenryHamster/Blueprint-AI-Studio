import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useRef, type MutableRefObject } from 'react';
import { useLogViewer } from './useLogViewer';
import type { StrategicStateCycle, StrategicStateResponse } from '../strategicStateApi';

vi.mock('../api', () => ({
  authHeaders: vi.fn().mockResolvedValue({ Authorization: 'Bearer ***' }),
  API_BASE_URL: '',
  fetchTask: vi.fn(async (id: string) => ({ id, project_id: 'projA', status: 'IN_PROGRESS', title: 'T' })),
}));

import { fetchTask } from '../api';

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
  url: string;
  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }
  emit(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent);
  }
}

let fetchSpy: ReturnType<typeof vi.fn>;
let failingTaskTypesFetchCount = 0;

function setup(options: {
  selectedProjectId?: string | null;
  realtimeWsEnabled?: boolean;
  strategicReadModelRef?: MutableRefObject<StrategicStateResponse | null>;
  canViewProject?: boolean;
  isAdmin?: boolean;
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
      canViewProject: options.canViewProject ?? true,
      isAdmin: options.isAdmin ?? true,
    });
    return { lv, fetchTasksRef, fetchDirectionsRef, refreshStrategicReadModelRef, refreshMessagesRef, fetchAlertsRef, refreshFleetRunnersRef };
  });
}

function makeCycle(overrides: Partial<StrategicStateCycle> = {}): StrategicStateCycle {
  return {
    id: 'cycle-reconnect',
    outcome: 'phase1_running',
    has_active_task: true,
    stale: false,
    phase1_task_id: 'phase-1',
    phase2_task_id: null,
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

function makeStrategicState(
  active_cycle: StrategicStateCycle | null,
  latest_cycle: StrategicStateCycle | null,
): StrategicStateResponse {
  return {
    project_id: 'proj1',
    strategic: {
      active_cycle,
      active_task: null,
      active_phase: null,
      latest_cycle,
      refresh: { pending: null },
    },
  } as StrategicStateResponse;
}

describe('useLogViewer strategist events', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    MockWebSocket.instances = [];
    failingTaskTypesFetchCount = 0;
    fetchSpy = vi.fn((url: RequestInfo | URL) => {
      const path = String(url);
      if (path.includes('/ws-ticket')) {
        return Promise.resolve({ ok: true, json: async () => ({ ticket: 't' }) });
      }
      if (path.includes('/failing-task-types')) {
        failingTaskTypesFetchCount += 1;
        const payload = failingTaskTypesFetchCount === 1
          ? {
              gated_types: [],
              global_failure_threshold: 0.6,
              anomaly_tightened: true,
              window_minutes: 15,
              generated_at: '2026-07-05T12:00:00Z',
            }
          : failingTaskTypesFetchCount === 2
            ? {
                gated_types: [
                  { task_type: 'create', failure_rate: 0.8, sample_size: 10, threshold: 0.6 },
                ],
                global_failure_threshold: 0.6,
                anomaly_tightened: true,
                window_minutes: 15,
                generated_at: '2026-07-05T12:00:00Z',
              }
            : failingTaskTypesFetchCount === 3
              ? {
                  gated_types: [
                    { task_type: 'create', failure_rate: 0.8, sample_size: 10, threshold: 0.6 },
                    { task_type: 'fix', failure_rate: 0.9, sample_size: 5, threshold: 0.6 },
                  ],
                  global_failure_threshold: 0.6,
                  anomaly_tightened: true,
                  window_minutes: 15,
                  generated_at: '2026-07-05T12:00:00Z',
                }
              : {
                  gated_types: [
                    { task_type: 'fix', failure_rate: 0.9, sample_size: 5, threshold: 0.6 },
                  ],
                  global_failure_threshold: 0.6,
                  anomaly_tightened: true,
                  window_minutes: 15,
                  generated_at: '2026-07-05T12:00:00Z',
                };
        return Promise.resolve({
          ok: true,
          json: async () => payload,
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

  async function connect(result: ReturnType<typeof setup>['result']) {
    // trackTask opens the WS for the project without needing a log drawer.
    // connectWs awaits a ticket fetch (two microtask points) before
    // constructing the WebSocket — flush microtasks, then fire onopen.
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

  it('connects the selected project websocket without opening logs or tracking a task', async () => {
    const { result } = setup({ selectedProjectId: 'proj-live' });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(MockWebSocket.instances).toHaveLength(1);
    const ws = MockWebSocket.instances[0];
    act(() => { ws.onopen?.({}); });
    const refreshMessages = result.current.refreshMessagesRef.current as ReturnType<typeof vi.fn>;
    refreshMessages.mockClear();

    act(() => { ws.emit({ event: 'message_created', data: { message_id: 'msg-1' } }); });
    act(() => { vi.advanceTimersByTime(500); });

    expect(refreshMessages).toHaveBeenCalledTimes(1);
  });

  it('does not fetch global failing-task-types for non-admin websocket activity', async () => {
    setup({ selectedProjectId: 'proj-live', isAdmin: false });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const ws = MockWebSocket.instances[0];
    act(() => { ws.onopen?.({}); });
    act(() => {
      ws.emit({ event: 'type_throttle', data: { project_id: 'proj-live', type: 'create' } });
      ws.emit({ event: 'type_unthrottle', data: { project_id: 'proj-live', type: 'create' } });
    });
    await act(async () => { await Promise.resolve(); });

    expect(failingTaskTypesFetchCount).toBe(0);
  });

  it('reconciles a canonical message projection immediately instead of waiting for the debounce', async () => {
    const { result } = setup({ selectedProjectId: 'proj-live' });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const ws = MockWebSocket.instances[0];
    act(() => { ws.onopen?.({}); });
    const refreshMessages = result.current.refreshMessagesRef.current as ReturnType<typeof vi.fn>;
    refreshMessages.mockClear();
    const canonical = {
      id: 'msg-2', project_id: 'proj-live', message_type: 'NOTE', from_type: 'strategic',
      from_task_id: null, content: 'durable reply', context: {}, status: 'open',
      resolution: null, resolved_at: null, created_at: '2026-07-18T00:00:00Z',
      sequence: 2, surface: 'conversation', client_idempotency_key: 'send-2',
    };

    act(() => { ws.emit({ event: 'message_created', data: { project_id: 'proj-live', message: canonical } }); });

    expect(refreshMessages).toHaveBeenCalledTimes(1);
    expect(refreshMessages).toHaveBeenCalledWith(canonical, false);
    expect(ws.send).toHaveBeenCalledWith(JSON.stringify({ type: 'message_ack', message_id: 'msg-2' }));
  });

  it('marks a truncated realtime message projection for canonical refetch', async () => {
    const { result } = setup({ selectedProjectId: 'proj-live' });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const ws = MockWebSocket.instances[0];
    act(() => { ws.onopen?.({}); });
    const refreshMessages = result.current.refreshMessagesRef.current as ReturnType<typeof vi.fn>;
    refreshMessages.mockClear();
    const canonical = {
      id: 'msg-old', project_id: 'proj-live', message_type: 'NOTE', from_type: 'strategic',
      from_task_id: null, content: '', context: { projection_truncated: true }, status: 'open',
      resolution: null, resolved_at: null, created_at: '2026-07-18T00:00:00Z',
      sequence: 2, surface: 'conversation', projection_truncated: true,
    };

    act(() => {
      ws.emit({
        event: 'message_updated',
        data: { project_id: 'proj-live', canonical_refetch_required: true, message: canonical },
      });
    });

    expect(refreshMessages).toHaveBeenCalledWith(canonical, true);
  });

  it('refreshes the conversation on websocket connect to close missed sequence gaps', async () => {
    const { result } = setup();
    const refreshMessages = result.current.refreshMessagesRef.current as ReturnType<typeof vi.fn>;
    expect(refreshMessages).not.toHaveBeenCalled();

    await connect(result);

    expect(refreshMessages).toHaveBeenCalledTimes(1);
  });

  it('routes pipeline refetch envelopes to the task refresh path', async () => {
    const { result } = setup({ selectedProjectId: 'proj-live' });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const ws = MockWebSocket.instances[0];
    act(() => { ws.onopen?.({}); });
    const fetchTasks = result.current.fetchTasksRef.current as ReturnType<typeof vi.fn>;
    fetchTasks.mockClear();

    act(() => {
      ws.emit({
        event: 'pipeline_refetched',
        data: {
          schema_version: 1,
          project_id: 'proj-live',
          resource: 'pipeline',
          action: 'updated',
          refetch: { surfaces: ['pipeline'] },
        },
      });
    });
    act(() => { vi.advanceTimersByTime(500); });

    expect(fetchTasks).toHaveBeenCalledTimes(1);
  });

  it('waits for realtime auth readiness before connecting the selected project websocket', async () => {
    const { rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => {
        const fetchTasksRef = useRef<() => void>(vi.fn());
        const fetchDirectionsRef = useRef<() => void>(vi.fn());
        const refreshMessagesRef = useRef<() => void>(vi.fn());
        const setDeployState = vi.fn();
        return useLogViewer({
          fetchTasksRef,
          setDeployState,
          fetchDirectionsRef,
          refreshMessagesRef,
          selectedProjectId: 'proj-live',
          realtimeWsEnabled: enabled,
          canViewProject: true,
        });
      },
      { initialProps: { enabled: false } },
    );

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(MockWebSocket.instances).toHaveLength(0);

    act(() => { rerender({ enabled: true }); });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it('keeps the selected project websocket open when tracked tasks finish', async () => {
    const { result } = setup({ selectedProjectId: 'proj-live' });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const ws = MockWebSocket.instances[0];
    act(() => { ws.onopen?.({}); });

    await act(async () => {
      result.current.lv.trackTask({ id: 'task-1', project_id: 'proj-live', status: 'IN_PROGRESS', title: 'T' } as never);
      await Promise.resolve();
    });

    act(() => {
      ws.emit({ event: 'task_updated', data: { task_id: 'task-1', status: 'COMPLETED' } });
    });

    expect(ws.close).not.toHaveBeenCalled();
    expect(result.current.lv.wsConnected).toBe(true);
  });

  it('closes a tracked project stream and ignores late events after view access is revoked', async () => {
    const { result, rerender } = renderHook(
      ({ canViewProject }: { canViewProject: boolean }) => {
        const fetchTasksRef = useRef<() => void>(vi.fn());
        const fetchDirectionsRef = useRef<() => void>(vi.fn());
        const refreshMessagesRef = useRef<() => void>(vi.fn());
        const setDeployState = vi.fn();
        const lv = useLogViewer({
          fetchTasksRef,
          setDeployState,
          fetchDirectionsRef,
          refreshMessagesRef,
          selectedProjectId: 'proj-live',
          canViewProject,
        });
        return { lv, fetchTasksRef, fetchDirectionsRef, refreshMessagesRef };
      },
      { initialProps: { canViewProject: true } },
    );
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const ws = MockWebSocket.instances[0];
    act(() => { ws.onopen?.({}); });
    act(() => {
      result.current.lv.trackTask({ id: 'task-1', project_id: 'proj-live', status: 'IN_PROGRESS', title: 'T' } as never);
    });
    const fetchTasks = result.current.fetchTasksRef.current as ReturnType<typeof vi.fn>;
    const fetchDirections = result.current.fetchDirectionsRef.current as ReturnType<typeof vi.fn>;
    const refreshMessages = result.current.refreshMessagesRef.current as ReturnType<typeof vi.fn>;
    fetchTasks.mockClear();
    fetchDirections.mockClear();
    refreshMessages.mockClear();
    const ticketRequestsBeforeRevocation = fetchSpy.mock.calls.filter(([url]) => String(url).includes('/ws-ticket')).length;

    act(() => { rerender({ canViewProject: false }); });
    expect(ws.close).toHaveBeenCalled();
    act(() => {
      ws.emit({ event: 'task_updated', data: { task_id: 'task-1', status: 'COMPLETED' } });
      ws.emit({ event: 'strategic_message_created', data: { project_id: 'proj-live' } });
      ws.onclose?.({ code: 1006, reason: 'late network close' });
      vi.advanceTimersByTime(35_000);
    });

    expect(fetchTasks).not.toHaveBeenCalled();
    expect(fetchDirections).not.toHaveBeenCalled();
    expect(refreshMessages).not.toHaveBeenCalled();
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(fetchSpy.mock.calls.filter(([url]) => String(url).includes('/ws-ticket'))).toHaveLength(ticketRequestsBeforeRevocation);
  });

  it('does not close a tracked task stream when the selected project changes', async () => {
    const { result, rerender } = renderHook(
      ({ pid }: { pid: string | null }) => {
        const fetchTasksRef = useRef<() => void>(vi.fn());
        const fetchDirectionsRef = useRef<() => void>(vi.fn());
        const refreshMessagesRef = useRef<() => void>(vi.fn());
        const setDeployState = vi.fn();
        const lv = useLogViewer({ fetchTasksRef, setDeployState, fetchDirectionsRef, refreshMessagesRef, selectedProjectId: pid, canViewProject: true, isAdmin: true });
        return { lv };
      },
      { initialProps: { pid: 'projA' as string | null } },
    );
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const wsA = MockWebSocket.instances[0];
    act(() => { wsA.onopen?.({}); });

    await act(async () => {
      result.current.lv.trackTask({ id: 'task-1', project_id: 'projA', status: 'IN_PROGRESS', title: 'T' } as never);
      await Promise.resolve();
    });
    act(() => { rerender({ pid: 'projB' }); });

    expect(wsA.close).not.toHaveBeenCalled();
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it('switches the background stream after terminal tracked tasks stop being foreground work', async () => {
    const { result, rerender } = renderHook(
      ({ pid }: { pid: string | null }) => {
        const fetchTasksRef = useRef<() => void>(vi.fn());
        const fetchDirectionsRef = useRef<() => void>(vi.fn());
        const refreshMessagesRef = useRef<() => void>(vi.fn());
        const setDeployState = vi.fn();
        const lv = useLogViewer({ fetchTasksRef, setDeployState, fetchDirectionsRef, refreshMessagesRef, selectedProjectId: pid, canViewProject: true });
        return { lv };
      },
      { initialProps: { pid: 'projA' as string | null } },
    );
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const wsA = MockWebSocket.instances[0];
    act(() => { wsA.onopen?.({}); });

    await act(async () => {
      result.current.lv.trackTask({ id: 'task-1', project_id: 'projA', status: 'IN_PROGRESS', title: 'T' } as never);
      await Promise.resolve();
    });
    act(() => {
      wsA.emit({ event: 'task_updated', data: { task_id: 'task-1', status: 'COMPLETED' } });
    });
    expect(wsA.close).not.toHaveBeenCalled();

    act(() => { rerender({ pid: 'projB' }); });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(wsA.close).toHaveBeenCalledWith(1000, 'Project changed');
    expect(MockWebSocket.instances).toHaveLength(2);
  });

  it('restores the selected project stream when a viewed log task completes', async () => {
    const { result } = setup({ selectedProjectId: 'projB' });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const wsB = MockWebSocket.instances[0];
    act(() => { wsB.onopen?.({}); });

    await act(async () => {
      result.current.lv.handleViewLogs({ id: 'task-log', project_id: 'projA', status: 'IN_PROGRESS', title: 'Log task' } as never);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const wsA = MockWebSocket.instances[1];
    act(() => { wsA.onopen?.({}); });

    act(() => {
      wsA.emit({ event: 'task_updated', data: { task_id: 'task-log', status: 'COMPLETED' } });
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(wsA.close).toHaveBeenCalledWith(1000, 'Log task completed');
    expect(MockWebSocket.instances).toHaveLength(3);
  });

  it('does not restore selected project stream after foreground work if realtime auth is disabled', async () => {
    const { result, rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => {
        const fetchTasksRef = useRef<() => void>(vi.fn());
        const fetchDirectionsRef = useRef<() => void>(vi.fn());
        const refreshMessagesRef = useRef<() => void>(vi.fn());
        const setDeployState = vi.fn();
        const lv = useLogViewer({
          fetchTasksRef,
          setDeployState,
          fetchDirectionsRef,
          refreshMessagesRef,
          selectedProjectId: 'projB',
          realtimeWsEnabled: enabled,
          canViewProject: true,
        });
        return { lv };
      },
      { initialProps: { enabled: true } },
    );
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    act(() => { MockWebSocket.instances[0].onopen?.({}); });

    await act(async () => {
      result.current.lv.trackTask({ id: 'task-1', project_id: 'projA', status: 'IN_PROGRESS', title: 'Foreground task' } as never);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const foregroundWs = MockWebSocket.instances[1];
    act(() => { foregroundWs.onopen?.({}); });
    act(() => { rerender({ enabled: false }); });

    act(() => {
      foregroundWs.emit({ event: 'task_updated', data: { task_id: 'task-1', status: 'COMPLETED' } });
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(MockWebSocket.instances).toHaveLength(2);
  });

  it('closeLogViewer does not restore selected project stream when realtime auth is disabled', async () => {
    const { result, rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => {
        const fetchTasksRef = useRef<() => void>(vi.fn());
        const fetchDirectionsRef = useRef<() => void>(vi.fn());
        const refreshMessagesRef = useRef<() => void>(vi.fn());
        const setDeployState = vi.fn();
        const lv = useLogViewer({
          fetchTasksRef,
          setDeployState,
          fetchDirectionsRef,
          refreshMessagesRef,
          selectedProjectId: 'projB',
          realtimeWsEnabled: enabled,
          canViewProject: true,
        });
        return { lv };
      },
      { initialProps: { enabled: true } },
    );
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    act(() => { MockWebSocket.instances[0].onopen?.({}); });

    await act(async () => {
      result.current.lv.handleViewLogs({ id: 'task-log', project_id: 'projA', status: 'IN_PROGRESS', title: 'Log task' } as never);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    act(() => { MockWebSocket.instances[1].onopen?.({}); });
    act(() => { rerender({ enabled: false }); });

    act(() => { result.current.lv.closeLogViewer(); });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(MockWebSocket.instances).toHaveLength(2);
  });

  it('non-streaming log statuses do not block selected project websocket changes', async () => {
    vi.mocked(fetchTask).mockResolvedValueOnce({ id: 'queued-log', project_id: 'projB', status: 'QUEUED', title: 'Queued log task' } as never);
    const { result, rerender } = renderHook(
      ({ pid }: { pid: string | null }) => {
        const fetchTasksRef = useRef<() => void>(vi.fn());
        const fetchDirectionsRef = useRef<() => void>(vi.fn());
        const refreshMessagesRef = useRef<() => void>(vi.fn());
        const setDeployState = vi.fn();
        const lv = useLogViewer({ fetchTasksRef, setDeployState, fetchDirectionsRef, refreshMessagesRef, selectedProjectId: pid, canViewProject: true });
        return { lv };
      },
      { initialProps: { pid: 'projA' as string | null } },
    );
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const wsA = MockWebSocket.instances[0];
    act(() => { wsA.onopen?.({}); });

    await act(async () => {
      result.current.lv.handleViewLogs({ id: 'queued-log', project_id: 'projB', status: 'QUEUED', title: 'Queued log task' } as never);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(MockWebSocket.instances).toHaveLength(1);

    act(() => { rerender({ pid: 'projC' }); });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(wsA.close).toHaveBeenCalledWith(1000, 'Project changed');
    expect(MockWebSocket.instances).toHaveLength(2);
  });

  it('restores selected project stream when a viewed log task stops streaming', async () => {
    const { result } = setup({ selectedProjectId: 'projB' });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    act(() => { MockWebSocket.instances[0].onopen?.({}); });

    await act(async () => {
      result.current.lv.handleViewLogs({ id: 'task-log', project_id: 'projA', status: 'IN_PROGRESS', title: 'Log task' } as never);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const wsA = MockWebSocket.instances[1];
    act(() => { wsA.onopen?.({}); });

    act(() => {
      wsA.emit({ event: 'task_updated', data: { task_id: 'task-log', status: 'EVALUATING' } });
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(wsA.close).toHaveBeenCalledWith(1000, 'Log task stopped streaming');
    expect(MockWebSocket.instances).toHaveLength(3);
  });

  it('restores selected project stream when fresh log task status is non-streaming', async () => {
    vi.mocked(fetchTask).mockResolvedValueOnce({ id: 'task-log', project_id: 'projA', status: 'EVALUATING', title: 'Log task' } as never);
    const { result } = setup({ selectedProjectId: 'projB' });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    act(() => { MockWebSocket.instances[0].onopen?.({}); });

    await act(async () => {
      result.current.lv.handleViewLogs({ id: 'task-log', project_id: 'projA', status: 'IN_PROGRESS', title: 'Stale log task' } as never);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(MockWebSocket.instances[0].close).not.toHaveBeenCalled();
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it('tracking a task from another project replaces the background selected-project stream', async () => {
    const { result } = setup({ selectedProjectId: 'projA' });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const wsA = MockWebSocket.instances[0];
    act(() => { wsA.onopen?.({}); });

    await act(async () => {
      result.current.lv.trackTask({ id: 'task-2', project_id: 'projB', status: 'IN_PROGRESS', title: 'Other project task' } as never);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(wsA.close).toHaveBeenCalledWith(1000, 'Tracking task project');
    expect(MockWebSocket.instances).toHaveLength(2);
  });

  it('tracking an already-terminal task does not replace the selected project stream', async () => {
    const { result } = setup({ selectedProjectId: 'projA' });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const wsA = MockWebSocket.instances[0];
    act(() => { wsA.onopen?.({}); });

    await act(async () => {
      result.current.lv.trackTask({ id: 'task-done', project_id: 'projB', status: 'COMPLETED', title: 'Done elsewhere' } as never);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(wsA.close).not.toHaveBeenCalled();
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it('tracking a non-streaming non-terminal task does not replace the selected project stream', async () => {
    const { result } = setup({ selectedProjectId: 'projA' });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const wsA = MockWebSocket.instances[0];
    act(() => { wsA.onopen?.({}); });

    await act(async () => {
      result.current.lv.trackTask({ id: 'task-parked', project_id: 'projB', status: 'PARKED', title: 'Parked elsewhere' } as never);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(wsA.close).not.toHaveBeenCalled();
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it('does not reconnect on online after intentional close with no consumers', async () => {
    const { result } = setup();
    const ws = await connect(result);
    act(() => {
      ws.emit({ event: 'task_updated', data: { task_id: 'task-1', status: 'COMPLETED' } });
    });
    expect(ws.close).toHaveBeenCalledWith(1000, 'Tracked tasks completed');

    act(() => { window.dispatchEvent(new Event('online')); });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it('does not reconnect on online after an intentional remote close', async () => {
    setup({ selectedProjectId: 'proj-live' });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const ws = MockWebSocket.instances[0];
    act(() => { ws.onopen?.({}); });

    act(() => { ws.onclose?.({ code: 1000, reason: 'remote normal close' }); });
    act(() => { window.dispatchEvent(new Event('online')); });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it('closeLogViewer does not disable heartbeat for the selected project stream', async () => {
    const { result } = setup({ selectedProjectId: 'proj-live' });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const ws = MockWebSocket.instances[0];
    act(() => { ws.onopen?.({}); });
    ws.send.mockClear();

    act(() => { result.current.lv.closeLogViewer(); });
    expect(ws.close).not.toHaveBeenCalled();

    act(() => { vi.advanceTimersByTime(30_000); });

    expect(ws.send).toHaveBeenCalledWith(JSON.stringify({ type: 'ping' }));
  });

  it('stale project close events still clear their own heartbeat', async () => {
    const { rerender } = renderHook(
      ({ pid }: { pid: string | null }) => {
        const fetchTasksRef = useRef<() => void>(vi.fn());
        const fetchDirectionsRef = useRef<() => void>(vi.fn());
        const refreshMessagesRef = useRef<() => void>(vi.fn());
        const setDeployState = vi.fn();
        return useLogViewer({ fetchTasksRef, setDeployState, fetchDirectionsRef, refreshMessagesRef, selectedProjectId: pid, canViewProject: true });
      },
      { initialProps: { pid: 'projA' as string | null } },
    );
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const wsA = MockWebSocket.instances[0];
    act(() => { wsA.onopen?.({}); });
    wsA.send.mockClear();

    act(() => { rerender({ pid: 'projB' }); });
    act(() => { wsA.onclose?.({ code: 1000, reason: 'Project changed' }); });
    act(() => { vi.advanceTimersByTime(30_000); });

    expect(wsA.send).not.toHaveBeenCalledWith(JSON.stringify({ type: 'ping' }));
  });

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

  it('debounces Strategic read-model refreshes for operator workbench events', async () => {
    const { result } = setup();
    const ws = await connect(result);
    const refreshStrategicReadModel = result.current.refreshStrategicReadModelRef.current as ReturnType<typeof vi.fn>;
    refreshStrategicReadModel.mockClear();

    act(() => {
      ws.emit({ event: 'task_updated', data: { task_id: 'task-1', status: 'IN_PROGRESS' } });
      ws.emit({ event: 'message_created', data: { message_id: 'msg-1' } });
      ws.emit({ event: 'alert_created', data: { alert_id: 'alert-1' } });
      ws.emit({ event: 'strategic_updated', data: { kind: 'running_update' } });
    });
    expect(refreshStrategicReadModel).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(499); });
    expect(refreshStrategicReadModel).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(1); });
    expect(refreshStrategicReadModel).toHaveBeenCalledTimes(1);
  });

  it('routes envelope refetch surfaces to messages and Strategic session refreshes', async () => {
    const { result } = setup({ selectedProjectId: 'proj1' });
    const ws = await connect(result);
    const refreshStrategicReadModel = result.current.refreshStrategicReadModelRef.current as ReturnType<typeof vi.fn>;
    const refreshMessages = result.current.refreshMessagesRef.current as ReturnType<typeof vi.fn>;
    refreshStrategicReadModel.mockClear();
    refreshMessages.mockClear();

    act(() => {
      ws.emit({
        event: 'workbench_refetch',
        data: {
          schema_version: 1,
          project_id: 'proj1',
          resource: 'project_message',
          action: 'created',
          refetch: {
            surfaces: ['messages', 'strategic_session'],
          },
        },
      });
    });

    expect(refreshStrategicReadModel).not.toHaveBeenCalled();
    expect(refreshMessages).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(500); });
    expect(refreshStrategicReadModel).toHaveBeenCalledTimes(1);
    expect(refreshMessages).toHaveBeenCalledTimes(1);
  });

  it('ignores v1 envelope refetch surfaces for another project', async () => {
    const { result } = setup({ selectedProjectId: 'proj1' });
    const ws = await connect(result);
    const refreshStrategicReadModel = result.current.refreshStrategicReadModelRef.current as ReturnType<typeof vi.fn>;
    const refreshMessages = result.current.refreshMessagesRef.current as ReturnType<typeof vi.fn>;
    refreshStrategicReadModel.mockClear();
    refreshMessages.mockClear();

    act(() => {
      ws.emit({
        event: 'workbench_refetch',
        data: {
          schema_version: 1,
          project_id: 'proj2',
          resource: 'project_message',
          action: 'created',
          refetch: {
            surfaces: ['messages', 'strategic_session'],
          },
        },
      });
    });

    act(() => { vi.advanceTimersByTime(500); });
    expect(refreshStrategicReadModel).not.toHaveBeenCalled();
    expect(refreshMessages).not.toHaveBeenCalled();
  });

  it('ignores envelope-bearing legacy project events for another project', async () => {
    const { result } = setup({ selectedProjectId: 'proj1' });
    const ws = await connect(result);
    const fetchDirections = result.current.fetchDirectionsRef.current as ReturnType<typeof vi.fn>;
    const refreshStrategicReadModel = result.current.refreshStrategicReadModelRef.current as ReturnType<typeof vi.fn>;
    const refreshMessages = result.current.refreshMessagesRef.current as ReturnType<typeof vi.fn>;
    fetchDirections.mockClear();
    refreshStrategicReadModel.mockClear();
    refreshMessages.mockClear();

    act(() => {
      ws.emit({
        event: 'message_created',
        data: {
          schema_version: 1,
          project_id: 'proj2',
          resource: 'project_message',
          action: 'created',
          refetch: {
            surfaces: ['messages', 'strategic_session'],
          },
        },
      });
      ws.emit({
        event: 'strategic_updated',
        data: {
          schema_version: 1,
          project_id: 'proj2',
          resource: 'strategic',
          action: 'updated',
          refetch: {
            surfaces: ['directions', 'strategic_session'],
          },
        },
      });
      ws.emit({
        event: 'strategic_cycle_started',
        data: {
          schema_version: 1,
          project_id: 'proj2',
          resource: 'strategic_cycle',
          action: 'started',
          task_id: 'task-foreign',
        },
      });
    });

    act(() => { vi.advanceTimersByTime(500); });
    expect(refreshStrategicReadModel).not.toHaveBeenCalled();
    expect(refreshMessages).not.toHaveBeenCalled();
    expect(fetchDirections).not.toHaveBeenCalled();
    expect(result.current.lv.strategistActive).toBe(false);
  });

  it('keeps legacy message events refreshing the message list', async () => {
    const { result } = setup();
    const ws = await connect(result);
    const refreshStrategicReadModel = result.current.refreshStrategicReadModelRef.current as ReturnType<typeof vi.fn>;
    const refreshMessages = result.current.refreshMessagesRef.current as ReturnType<typeof vi.fn>;

    for (const eventName of ['message_created', 'message_updated', 'messages_superseded']) {
      refreshStrategicReadModel.mockClear();
      refreshMessages.mockClear();

      act(() => { ws.emit({ event: eventName, data: { message_id: 'msg-1' } }); });

      expect(refreshStrategicReadModel, eventName).not.toHaveBeenCalled();
      expect(refreshMessages, eventName).not.toHaveBeenCalled();
      act(() => { vi.advanceTimersByTime(500); });
      expect(refreshStrategicReadModel, eventName).toHaveBeenCalledTimes(1);
      expect(refreshMessages, eventName).toHaveBeenCalledTimes(1);
    }
  });

  it('routes envelope directions surfaces to the directions fetcher', async () => {
    const { result } = setup();
    const ws = await connect(result);
    const fetchDirections = result.current.fetchDirectionsRef.current as ReturnType<typeof vi.fn>;
    const refreshStrategicReadModel = result.current.refreshStrategicReadModelRef.current as ReturnType<typeof vi.fn>;
    const refreshMessages = result.current.refreshMessagesRef.current as ReturnType<typeof vi.fn>;
    fetchDirections.mockClear();
    refreshStrategicReadModel.mockClear();
    refreshMessages.mockClear();

    act(() => {
      ws.emit({
        event: 'workbench_refetch',
        data: {
          refetch: {
            surfaces: ['directions'],
          },
        },
      });
    });

    act(() => { vi.advanceTimersByTime(500); });
    expect(fetchDirections).toHaveBeenCalledTimes(1);
    expect(refreshStrategicReadModel).not.toHaveBeenCalled();
    expect(refreshMessages).not.toHaveBeenCalled();
  });

  it('refreshes the Strategic read model for every known workbench-changing event', async () => {
    const { result } = setup();
    const ws = await connect(result);
    const refreshStrategicReadModel = result.current.refreshStrategicReadModelRef.current as ReturnType<typeof vi.fn>;
    refreshStrategicReadModel.mockClear();

    const events = [
      'task_updated',
      'artifact_created',
      'alert_created',
      'alert_dismissed',
      'message_created',
      'message_updated',
      'messages_superseded',
      'strategic_updated',
      'strategic_cycle_started',
      'strategic_cycle_completed',
    ];

    for (const eventName of events) {
      refreshStrategicReadModel.mockClear();
      act(() => { ws.emit({ event: eventName, data: { task_id: 'task-1' } }); });
      act(() => { vi.advanceTimersByTime(500); });
      expect(refreshStrategicReadModel, eventName).toHaveBeenCalledTimes(1);
    }
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

  it('uses the durable phase after reconnect while retaining only live steering metadata', async () => {
    const strategicReadModelRef = {
      current: makeStrategicState(makeCycle(), null),
    };
    const { result } = setup({ selectedProjectId: 'proj1', strategicReadModelRef });
    const ws = await connect(result);

    act(() => {
      ws.emit({
        event: 'strategic_cycle_started',
        data: {
          task_id: 'phase-1',
          cycle_id: 'cycle-reconnect',
          phase_number: 1,
          phase_label: 'Architecture review',
          phase_state: 'phase1_running',
          steering_origin: 'operator',
          steering_excerpt: 'Keep this steering context',
        },
      });
    });

    strategicReadModelRef.current = makeStrategicState(makeCycle({
      outcome: 'phase2_running',
      phase2_task_id: 'phase-2',
    }), null);
    act(() => { ws.onopen?.({}); });

    expect(result.current.lv.cycleProgress).toMatchObject({
      cycle_id: 'cycle-reconnect',
      task_id: 'phase-2',
      phase_number: 2,
      phase_label: 'Strategic review',
      phase_state: 'phase2_running',
      steering_origin: 'operator',
      steering_excerpt: 'Keep this steering context',
    });
  });

  it('replaces a same-cycle deferred receipt with durable failure and retains only unavailable live details', async () => {
    const strategicReadModelRef = {
      current: makeStrategicState(makeCycle(), null),
    };
    const { result } = setup({ selectedProjectId: 'proj1', strategicReadModelRef });
    const ws = await connect(result);

    act(() => {
      ws.emit({
        event: 'strategic_cycle_started',
        data: { task_id: 'phase-1', cycle_id: 'cycle-reconnect', phase_number: 1 },
      });
      ws.emit({
        event: 'strategic_cycle_completed',
        data: {
          cycle_id: 'cycle-reconnect',
          completion: {
            status: 'deferred',
            created_count: 3,
            strategic_judgment_challenge: {
              mode: 'ok',
              verdict: 'warned',
              warned_count: 1,
              blocked_count: 0,
            },
          },
        },
      });
    });

    strategicReadModelRef.current = makeStrategicState(null, makeCycle({
      outcome: 'failed_phase2',
      has_active_task: false,
      summary_text: 'Durable phase-two failure',
      failure_reason: 'phase-two spawn failed',
    }));
    act(() => { ws.onopen?.({}); });

    expect(result.current.lv.lastCycleCompletion).toMatchObject({
      status: 'FAILED',
      created_count: 0,
      status_message: 'Durable phase-two failure',
      failure_reason: 'phase-two spawn failed',
      strategic_judgment_challenge: {
        mode: 'ok',
        verdict: 'warned',
        warned_count: 1,
        blocked_count: 0,
      },
    });
    expect(result.current.lv.lastCycleCompletion).not.toMatchObject({
      status: 'deferred',
      created_count: 3,
    });
  });

  it('resets strategistActive to false when the selected project changes', async () => {
    const { result, rerender } = renderHook(
      ({ pid }: { pid: string | null }) => {
        const fetchTasksRef = useRef<() => void>(vi.fn());
        const fetchDirectionsRef = useRef<() => void>(vi.fn());
        const setDeployState = vi.fn();
        const lv = useLogViewer({ fetchTasksRef, setDeployState, fetchDirectionsRef, selectedProjectId: pid, canViewProject: true });
        return { lv, fetchDirectionsRef };
      },
      { initialProps: { pid: 'projA' as string | null } },
    );

    await act(async () => {
      result.current.lv.trackTask({ id: 'task-1', project_id: 'projA', status: 'IN_PROGRESS', title: 'T' } as never);
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
    const refreshStrategicReadModel = result.current.refreshStrategicReadModelRef.current as ReturnType<typeof vi.fn>;
    fetchDirections.mockClear();
    refreshStrategicReadModel.mockClear();

    act(() => { ws.emit({ event: 'pong' }); });
    act(() => { vi.advanceTimersByTime(500); });
    expect(fetchDirections).not.toHaveBeenCalled();
    expect(refreshStrategicReadModel).not.toHaveBeenCalled();
    expect(result.current.lv.strategistActive).toBe(false);
  });

  it('debounces a fetchAlerts call on alert_created event', async () => {
    const { result } = setup({ selectedProjectId: 'proj1' });
    const ws = await connect(result);
    const fetchAlerts = result.current.fetchAlertsRef.current as ReturnType<typeof vi.fn>;
    fetchAlerts.mockClear();

    act(() => { ws.emit({ event: 'alert_created', data: { alert_id: 'a-1' } }); });
    expect(fetchAlerts).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(500); });
    expect(fetchAlerts).toHaveBeenCalledTimes(1);
  });

  it('debounces a fetchAlerts call on alert_dismissed event', async () => {
    const { result } = setup({ selectedProjectId: 'proj1' });
    const ws = await connect(result);
    const fetchAlerts = result.current.fetchAlertsRef.current as ReturnType<typeof vi.fn>;
    fetchAlerts.mockClear();

    act(() => { ws.emit({ event: 'alert_dismissed', data: { alert_id: 'a-1' } }); });
    expect(fetchAlerts).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(500); });
    expect(fetchAlerts).toHaveBeenCalledTimes(1);
  });

  it('debounces a fetchAlerts call on workbench_refetch with runtime surface', async () => {
    const { result } = setup({ selectedProjectId: 'proj1' });
    const ws = await connect(result);
    const fetchAlerts = result.current.fetchAlertsRef.current as ReturnType<typeof vi.fn>;
    const refreshFleetRunners = result.current.refreshFleetRunnersRef.current as ReturnType<typeof vi.fn>;
    fetchAlerts.mockClear();
    refreshFleetRunners.mockClear();

    act(() => {
      ws.emit({
        event: 'workbench_refetch',
        data: {
          schema_version: 1,
          project_id: 'proj1',
          resource: 'runner',
          action: 'updated',
          refetch: { surfaces: ['runtime'] },
        },
      });
    });
    expect(fetchAlerts).not.toHaveBeenCalled();
    expect(refreshFleetRunners).toHaveBeenCalledTimes(1);
    act(() => { vi.advanceTimersByTime(500); });
    expect(fetchAlerts).toHaveBeenCalledTimes(1);
  });

  it('debounces multiple alert events into a single fetchAlerts call', async () => {
    const { result } = setup({ selectedProjectId: 'proj1' });
    const ws = await connect(result);
    const fetchAlerts = result.current.fetchAlertsRef.current as ReturnType<typeof vi.fn>;
    fetchAlerts.mockClear();

    act(() => {
      ws.emit({ event: 'alert_created', data: { alert_id: 'a-1' } });
      ws.emit({ event: 'alert_dismissed', data: { alert_id: 'a-2' } });
    });
    act(() => { vi.advanceTimersByTime(500); });
    expect(fetchAlerts).toHaveBeenCalledTimes(1);
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

    // Emit the full canonical v1 completion envelope the backend sends. Its
    // directions refetch surface must not prevent this lifecycle event from
    // clearing the active state and recording its receipt.
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
            // Compact strategic-judgment-challenge summary rides the completion
            // sub-object verbatim and must be captured for the receipt chip.
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
      strategic_judgment_challenge: {
        mode: 'ok',
        verdict: 'warned',
        warned_count: 1,
        blocked_count: 0,
      },
    });

    // Directions refetch must have been debounced and fired.
    const fetchDirections = result.current.fetchDirectionsRef.current as ReturnType<typeof vi.fn>;
    act(() => { vi.advanceTimersByTime(500); });
    expect(fetchDirections).toHaveBeenCalledTimes(1);
  });

  it('adds a gated type on type_throttle and removes it on type_unthrottle', async () => {
    const { result } = setup({ selectedProjectId: 'proj-live' });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const ws = MockWebSocket.instances[0];
    act(() => { ws.onopen?.({}); });

    expect(result.current.lv.gatedTaskTypes).toHaveLength(0);

    act(() => {
      ws.emit({ event: 'type_throttle', data: { type: 'create', failure_rate: 0.8, sample_size: 10, detail: 'gated' } });
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.lv.gatedTaskTypes).toHaveLength(1);
    expect(result.current.lv.gatedTaskTypes[0].task_type).toBe('create');
    expect(result.current.lv.gatedTaskTypes[0].failure_rate).toBe(0.8);
    expect(result.current.lv.gatedTaskTypes[0].sample_size).toBe(10);
    expect(result.current.lv.gatedTaskTypes[0].threshold).toBe(0.6);

    act(() => {
      ws.emit({ event: 'type_throttle', data: { type: 'fix', failure_rate: 0.9, sample_size: 5, detail: 'gated' } });
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.lv.gatedTaskTypes).toHaveLength(2);
    // Sorted alphabetically
    expect(result.current.lv.gatedTaskTypes[0].task_type).toBe('create');
    expect(result.current.lv.gatedTaskTypes[1].task_type).toBe('fix');

    act(() => {
      ws.emit({ event: 'type_unthrottle', data: { type: 'create', failure_rate: 0.5, sample_size: 10, detail: 'recovered' } });
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.lv.gatedTaskTypes).toHaveLength(1);
    expect(result.current.lv.gatedTaskTypes[0].task_type).toBe('fix');
  });

  it('clears gated types on project change', async () => {
    const { result, rerender } = renderHook(
      ({ pid }: { pid: string | null }) => {
        const fetchTasksRef = useRef<() => void>(vi.fn());
        const fetchDirectionsRef = useRef<() => void>(vi.fn());
        const refreshMessagesRef = useRef<() => void>(vi.fn());
        const setDeployState = vi.fn();
        const lv = useLogViewer({ fetchTasksRef, setDeployState, fetchDirectionsRef, refreshMessagesRef, selectedProjectId: pid, canViewProject: true, isAdmin: true });
        return { lv };
      },
      { initialProps: { pid: 'projA' as string | null } },
    );
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const ws = MockWebSocket.instances[0];
    act(() => { ws.onopen?.({}); });

    act(() => {
      ws.emit({ event: 'type_throttle', data: { type: 'create', failure_rate: 0.8, sample_size: 10, detail: 'gated' } });
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.lv.gatedTaskTypes).toHaveLength(1);

    act(() => { rerender({ pid: 'projB' }); });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.lv.gatedTaskTypes).toHaveLength(0);
  });
});
