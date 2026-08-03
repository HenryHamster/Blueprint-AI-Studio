import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, renderHook, cleanup, waitFor } from '@testing-library/react';
import { useStrategicSession as useStrategicSessionHook } from './useStrategicSession';
import {
  checkInStrategicSession, startStrategicSession, stopStrategicSession, updateStrategicWorkbenchState,
} from '../strategicStateApi';
import type {
  StrategicLivenessResponse,
  StrategicSessionStartResponse,
  StrategicStateResponse,
} from '../strategicStateApi';

// Spread the real modules and override only the members under test so the
// mock-factory-export-coverage scanner treats these full-replacement mocks as
// complete (importOriginal delegation). The overridden handlers are never
// invoked here — the tests only read derived state — but keeping the real
// exports avoids partial-mock drift.
vi.mock('../strategicStateApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../strategicStateApi')>()),
  checkInStrategicSession: vi.fn(),
  startStrategicSession: vi.fn(),
  stopStrategicSession: vi.fn(),
  updateStrategicWorkbenchState: vi.fn(),
}));
vi.mock('../utils/strategicContextDescriptor', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/strategicContextDescriptor')>()),
  buildStrategicContextDescriptor: vi.fn(() => null),
}));

type StrategicSessionOptions = Parameters<typeof useStrategicSessionHook>[0];
type ReadModel = StrategicSessionOptions['strategicReadModel'];

function useStrategicSession(options: Omit<StrategicSessionOptions, 'canOperateProject'> & { canOperateProject?: boolean }) {
  return useStrategicSessionHook({ canOperateProject: true, ...options });
}

function makeReadModel(data: StrategicStateResponse | null): ReadModel {
  return {
    data,
    loading: false,
    error: null,
    refresh: vi.fn().mockResolvedValue(undefined),
    applySessionResponse: vi.fn(),
  } as unknown as ReadModel;
}

function makeLiveness(): StrategicLivenessResponse {
  return {
    project_id: 'proj-1',
    interaction_mode: 'continuous',
    resident_session: { session_id: 'ss-live', status: 'active', stop_requested: false },
    resident_session_availability: { available: true, state: 'active', reason: null },
    resident_session_runners: [
      { id: 'r-1', name: 'desk', online: true, agent_ready: true, eligible: true },
    ],
  };
}

function successfulStartResponse(): StrategicSessionStartResponse {
  return {
    project_id: 'proj-1',
    ensure_outcome: 'ensured',
    session: {
      session_id: 'ss-live',
      status: 'active',
      worker_state: 'ready',
      started_at: new Date().toISOString(),
    },
  };
}

// Minimal heavy read-model state carrying its own (different) resident-session
// fields so we can prove the read model wins once it lands.
function makeHeavyState(): StrategicStateResponse {
  return {
    project_id: 'proj-1',
    strategic: { active_cycle: null, active_task: null, active_phase: null, latest_cycle: null, refresh: { pending: null } },
    resident_session: { session_id: 'ss-heavy', status: 'active', stop_requested: false },
    resident_session_availability: { available: true, state: 'starting', reason: null },
    resident_session_runners: [],
    workbench: {
      version: 1,
      interaction_mode: 'chat',
      active_surface: 'strategic',
      selected_refs: [],
    },
    running_update: { excerpt: null, char_count: 0, acknowledged_at: null },
    problem_model: { active_gap_count: 0, capability_count: 0, pending_confirmation_count: 0, top_gap: null },
    directions: { total_count: 0, active_count: 0, paused_count: 0, primary: null },
    messages: { counts: { open: 0, linked: 0 }, items: [] },
    tasks: { counts: { queued: 0, running: 0, evaluating: 0, blocked: 0, parked: 0, failed: 0, completed: 0, total: 0 }, cumulative_estimated_cost_usd: 0 },
    design: { has_design_doc: false },
  } as StrategicStateResponse;
}

describe('useStrategicSession liveness fallback', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });
  afterEach(() => {
    vi.useRealTimers();
    cleanup();
  });

  it('resolves mode + session fields from the liveness probe before the read model lands', () => {
    const { result } = renderHook(() =>
      useStrategicSession({
        selectedProjectId: 'proj-1',
        addToast: vi.fn(),
        strategicReadModel: makeReadModel(null),
        strategicLiveness: makeLiveness(),
      }),
    );
    expect(result.current.strategicInteractionMode).toBe('continuous');
    expect(result.current.residentStrategicSession?.session_id).toBe('ss-live');
    expect(result.current.strategicSessionActive).toBe(true);
    expect(result.current.strategicSessionState).toBe('active');
    expect(result.current.residentStrategicSessionAvailable).toBe(true);
    expect(result.current.residentStrategicSessionRunners).toHaveLength(1);
  });

  it('lets the heavy read model take precedence once it hydrates', () => {
    const { result } = renderHook(() =>
      useStrategicSession({
        selectedProjectId: 'proj-1',
        addToast: vi.fn(),
        strategicReadModel: makeReadModel(makeHeavyState()),
        strategicLiveness: makeLiveness(),
      }),
    );
    // Read model wins: session id, availability state, and mode all come from it.
    expect(result.current.residentStrategicSession?.session_id).toBe('ss-heavy');
    expect(result.current.strategicSessionState).toBe('starting');
    expect(result.current.strategicInteractionMode).toBe('chat');
  });

  it('ignores a liveness probe for a different project', () => {
    const other: StrategicLivenessResponse = { ...makeLiveness(), project_id: 'proj-2' };
    const { result } = renderHook(() =>
      useStrategicSession({
        selectedProjectId: 'proj-1',
        addToast: vi.fn(),
        strategicReadModel: makeReadModel(null),
        strategicLiveness: other,
      }),
    );
    expect(result.current.residentStrategicSession).toBeNull();
    expect(result.current.strategicInteractionMode).toBeNull();
    expect(result.current.strategicSessionState).toBe('off');
  });

  it('idempotently ensures Workday once when reload hydrates directly into continuous mode', async () => {
    vi.mocked(startStrategicSession).mockResolvedValue(successfulStartResponse());
    const readModel = makeReadModel(null);
    const { rerender } = renderHook(() =>
      useStrategicSession({
        selectedProjectId: 'proj-1',
        addToast: vi.fn(),
        strategicReadModel: readModel,
        strategicLiveness: makeLiveness(),
      }),
    );

    await waitFor(() => expect(startStrategicSession).toHaveBeenCalledTimes(1));
    expect(startStrategicSession).toHaveBeenCalledWith('proj-1', {
      expected_interaction_mode: 'continuous',
    });
    await waitFor(() => expect(readModel.refresh).toHaveBeenCalledTimes(1));

    rerender();
    expect(startStrategicSession).toHaveBeenCalledTimes(1);
  });

  it('does not start, retry, or mutate Strategic session state for a viewer', async () => {
    const readModel = makeReadModel(null);
    const { result } = renderHook(() =>
      useStrategicSession({
        selectedProjectId: 'proj-1',
        canOperateProject: false,
        addToast: vi.fn(),
        strategicReadModel: readModel,
        strategicLiveness: makeLiveness(),
      }),
    );

    act(() => {
      result.current.persistStrategicWorkbench({ interaction_mode: 'continuous' });
      result.current.handleSelectStrategicRunner('runner-1');
      result.current.updateStrategicSessionRefs(() => [{ kind: 'task', id: 'task-1' }]);
    });
    await act(async () => {
      await result.current.handleStrategicCheckIn();
      await result.current.handleStartStrategicSession();
      await result.current.handleStopStrategicSession();
      await vi.advanceTimersByTimeAsync(10 * 60_000);
    });

    expect(startStrategicSession).not.toHaveBeenCalled();
    expect(stopStrategicSession).not.toHaveBeenCalled();
    expect(checkInStrategicSession).not.toHaveBeenCalled();
    expect(updateStrategicWorkbenchState).not.toHaveBeenCalled();
    expect(readModel.refresh).not.toHaveBeenCalled();
  });

  it('lets explicit runner selection own a live Chat to Workday start', async () => {
    const liveness = makeLiveness();
    liveness.interaction_mode = 'chat';
    liveness.resident_session = null;
    liveness.resident_session_availability = {
      available: true, state: 'ready', reason: null,
    };
    let resolveSave!: (value: Awaited<ReturnType<typeof updateStrategicWorkbenchState>>) => void;
    vi.mocked(updateStrategicWorkbenchState).mockReturnValue(
      new Promise(resolve => { resolveSave = resolve; }),
    );
    vi.mocked(startStrategicSession).mockResolvedValue(successfulStartResponse());
    const { result } = renderHook(() =>
      useStrategicSession({
        selectedProjectId: 'proj-1',
        addToast: vi.fn(),
        strategicReadModel: makeReadModel(null),
        strategicLiveness: liveness,
      }),
    );

    act(() => {
      result.current.persistStrategicWorkbench({
        interaction_mode: 'continuous', active_surface: 'strategic',
      });
      result.current.handleSelectStrategicRunner('hamsterwheel-runner');
    });
    const start = result.current.handleStartStrategicSession();
    await Promise.resolve();
    expect(startStrategicSession).not.toHaveBeenCalled();

    resolveSave({
      workbench: {
        version: 1,
        interaction_mode: 'continuous',
        active_surface: 'strategic',
        selected_refs: [],
        preferred_runner_id: 'hamsterwheel-runner',
      },
      arming: null,
    });
    await act(async () => { await start; });

    expect(startStrategicSession).toHaveBeenCalledTimes(1);
    expect(startStrategicSession).toHaveBeenCalledWith('proj-1', {
      runner_id: 'hamsterwheel-runner',
    });
  });

  it('does not clear explicit-start ownership against a stale predecessor snapshot', async () => {
    const stale = makeLiveness();
    stale.interaction_mode = 'chat';
    stale.resident_session = {
      session_id: 'ss-old', worker_generation: 1, status: 'ended',
      worker_state: 'restart_failed', stop_requested: false,
    };
    vi.mocked(updateStrategicWorkbenchState).mockResolvedValue({
      workbench: {
        version: 1, interaction_mode: 'continuous', active_surface: 'strategic',
        selected_refs: [], preferred_runner_id: 'hamsterwheel-runner',
      },
      arming: null,
    });
    vi.mocked(startStrategicSession).mockResolvedValue({
      ...successfulStartResponse(),
      session: {
        ...successfulStartResponse().session,
        session_id: 'ss-new', worker_generation: 2,
      },
    });
    const { result, rerender } = renderHook(() =>
      useStrategicSession({
        selectedProjectId: 'proj-1', addToast: vi.fn(),
        strategicReadModel: makeReadModel(null), strategicLiveness: stale,
      }),
    );

    act(() => {
      result.current.persistStrategicWorkbench({ interaction_mode: 'continuous' });
      result.current.handleSelectStrategicRunner('hamsterwheel-runner');
    });
    await act(async () => { await result.current.handleStartStrategicSession(); });
    rerender();
    await vi.advanceTimersByTimeAsync(1_000);
    stale.interaction_mode = 'continuous';
    stale.resident_session = {
      session_id: 'ss-new', worker_generation: 2, status: 'active',
      worker_state: 'ready', stop_requested: false,
      runner_id: 'hamsterwheel-runner',
    };
    rerender();
    await vi.advanceTimersByTimeAsync(1_000);

    expect(startStrategicSession).toHaveBeenCalledTimes(1);
  });

  it('reconciles a failed runner save before explicit Start reads the selection', async () => {
    const liveness = makeLiveness();
    liveness.interaction_mode = 'chat';
    liveness.resident_session = null;
    vi.mocked(updateStrategicWorkbenchState).mockRejectedValue(new Error('save failed'));
    vi.mocked(startStrategicSession).mockResolvedValue(successfulStartResponse());
    const { result } = renderHook(() =>
      useStrategicSession({
        selectedProjectId: 'proj-1', addToast: vi.fn(),
        strategicReadModel: makeReadModel(null), strategicLiveness: liveness,
      }),
    );

    act(() => result.current.handleSelectStrategicRunner('hidden-failed-runner'));
    await act(async () => { await result.current.handleStartStrategicSession(); });

    expect(startStrategicSession).toHaveBeenCalledWith('proj-1', {});
  });

  it('does not turn a rejected explicit Start into an autonomous retry loop', async () => {
    const liveness = makeLiveness();
    liveness.interaction_mode = 'chat';
    liveness.resident_session = null;
    vi.mocked(updateStrategicWorkbenchState).mockResolvedValue({
      workbench: {
        version: 1, interaction_mode: 'continuous', active_surface: 'strategic',
        selected_refs: [], preferred_runner_id: 'offline-runner',
      },
      arming: null,
    });
    vi.mocked(startStrategicSession).mockRejectedValue(new Error('runner offline'));
    const { result } = renderHook(() =>
      useStrategicSession({
        selectedProjectId: 'proj-1', addToast: vi.fn(),
        strategicReadModel: makeReadModel(null), strategicLiveness: liveness,
      }),
    );

    act(() => {
      result.current.persistStrategicWorkbench({ interaction_mode: 'continuous' });
      result.current.handleSelectStrategicRunner('offline-runner');
    });
    await act(async () => { await result.current.handleStartStrategicSession(); });
    await vi.advanceTimersByTimeAsync(30_000);

    expect(startStrategicSession).toHaveBeenCalledTimes(1);
  });

  it('retries a transient Workday ensure failure and stops after recovery', async () => {
    vi.mocked(startStrategicSession)
      .mockRejectedValueOnce(new Error('temporary conflict'))
      .mockResolvedValue(successfulStartResponse());
    const readModel = makeReadModel(null);
    renderHook(() =>
      useStrategicSession({
        selectedProjectId: 'proj-1',
        addToast: vi.fn(),
        strategicReadModel: readModel,
        strategicLiveness: makeLiveness(),
      }),
    );

    await waitFor(() => expect(startStrategicSession).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(readModel.refresh).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(350);
    expect(startStrategicSession).toHaveBeenCalledTimes(2);
  });

  it('retries Workday entry when the committed mode has not caught up yet', async () => {
    vi.mocked(startStrategicSession)
      .mockResolvedValueOnce({
        project_id: 'proj-1',
        session: null,
        ensure_outcome: 'mode_changed',
      })
      .mockResolvedValue({
        ...successfulStartResponse(),
      });
    const readModel = makeReadModel(null);
    renderHook(() =>
      useStrategicSession({
        selectedProjectId: 'proj-1',
        addToast: vi.fn(),
        strategicReadModel: readModel,
        strategicLiveness: makeLiveness(),
      }),
    );

    await waitFor(() => expect(startStrategicSession).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(readModel.refresh).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(350);
    expect(startStrategicSession).toHaveBeenCalledTimes(2);
  });

  it.each([
    ['null session', null],
    ['all-empty session', {}],
  ])('retries a 200 response with %s instead of completing the ensure epoch', async (_label, session) => {
    vi.mocked(startStrategicSession)
      .mockResolvedValueOnce({
        project_id: 'proj-1',
        session: session as StrategicSessionStartResponse['session'],
        ensure_outcome: 'ensured',
      })
      .mockResolvedValue(successfulStartResponse());
    const readModel = makeReadModel(null);
    const { rerender } = renderHook(() =>
      useStrategicSession({
        selectedProjectId: 'proj-1',
        addToast: vi.fn(),
        strategicReadModel: readModel,
        strategicLiveness: makeLiveness(),
      }),
    );

    await waitFor(() => expect(startStrategicSession).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(readModel.refresh).toHaveBeenCalledTimes(1));
    rerender();
    await vi.advanceTimersByTimeAsync(600);
    expect(startStrategicSession).toHaveBeenCalledTimes(2);
  });

  it('does not duplicate an in-flight ensure when the read-model wrapper changes', async () => {
    let resolveStart!: (response: StrategicSessionStartResponse) => void;
    const inFlightStart = new Promise<StrategicSessionStartResponse>(resolve => {
      resolveStart = resolve;
    });
    vi.mocked(startStrategicSession).mockReturnValue(inFlightStart);
    const stableReadModel = makeReadModel(null);
    const { rerender } = renderHook(
      ({ loading }: { loading: boolean }) =>
        useStrategicSession({
          selectedProjectId: 'proj-1',
          addToast: vi.fn(),
          strategicReadModel: { ...stableReadModel, loading } as ReadModel,
          strategicLiveness: makeLiveness(),
        }),
      { initialProps: { loading: false } },
    );

    await waitFor(() => expect(startStrategicSession).toHaveBeenCalledTimes(1));
    rerender({ loading: true });
    rerender({ loading: false });
    expect(startStrategicSession).toHaveBeenCalledTimes(1);

    resolveStart(successfulStartResponse());
    await waitFor(() => expect(stableReadModel.refresh).toHaveBeenCalledTimes(1));
    expect(startStrategicSession).toHaveBeenCalledTimes(1);
  });

  it('cancels a pending Workday ensure retry when leaving continuous mode', async () => {
    vi.mocked(startStrategicSession).mockRejectedValue(new Error('temporary conflict'));
    const readModel = makeReadModel(null);
    const initialLiveness = makeLiveness();
    const { rerender } = renderHook(
      ({ liveness }: { liveness: StrategicLivenessResponse }) =>
        useStrategicSession({
          selectedProjectId: 'proj-1',
          addToast: vi.fn(),
          strategicReadModel: readModel,
          strategicLiveness: liveness,
        }),
      { initialProps: { liveness: initialLiveness } },
    );

    await waitFor(() => expect(startStrategicSession).toHaveBeenCalledTimes(1));
    rerender({
      liveness: {
        ...initialLiveness,
        interaction_mode: 'chat',
      },
    });
    await vi.advanceTimersByTimeAsync(350);
    expect(startStrategicSession).toHaveBeenCalledTimes(1);
  });

  it('treats an in-flight mode-change no-op as cancellation without refresh or retry', async () => {
    let resolveStart!: (response: StrategicSessionStartResponse) => void;
    vi.mocked(startStrategicSession).mockReturnValue(
      new Promise<StrategicSessionStartResponse>(resolve => {
        resolveStart = resolve;
      }),
    );
    const readModel = makeReadModel(null);
    const initialLiveness = makeLiveness();
    const { rerender } = renderHook(
      ({ liveness }: { liveness: StrategicLivenessResponse }) =>
        useStrategicSession({
          selectedProjectId: 'proj-1',
          addToast: vi.fn(),
          strategicReadModel: readModel,
          strategicLiveness: liveness,
        }),
      { initialProps: { liveness: initialLiveness } },
    );

    await waitFor(() => expect(startStrategicSession).toHaveBeenCalledTimes(1));
    expect(startStrategicSession).toHaveBeenCalledWith('proj-1', {
      expected_interaction_mode: 'continuous',
    });
    rerender({
      liveness: {
        ...initialLiveness,
        interaction_mode: 'chat',
      },
    });
    resolveStart({
      project_id: 'proj-1',
      session: null,
      ensure_outcome: 'mode_changed',
    });
    await vi.advanceTimersByTimeAsync(350);

    expect(startStrategicSession).toHaveBeenCalledTimes(1);
    expect(readModel.refresh).not.toHaveBeenCalled();
  });

  it('keeps an explicitly stopped Workday stopped until the operator explicitly starts it', async () => {
    vi.mocked(startStrategicSession).mockResolvedValue(successfulStartResponse());
    vi.mocked(stopStrategicSession).mockResolvedValue({
      project_id: 'proj-1',
      session: {
        session_id: 'ss-live',
        status: 'ended',
        stop_requested: true,
      },
    });
    const readModel = makeReadModel(null);
    const initial = makeLiveness();
    const { result, rerender } = renderHook(
      ({ liveness }: { liveness: StrategicLivenessResponse }) =>
        useStrategicSession({
          selectedProjectId: 'proj-1',
          addToast: vi.fn(),
          strategicReadModel: readModel,
          strategicLiveness: liveness,
        }),
      { initialProps: { liveness: initial } },
    );
    await waitFor(() => expect(startStrategicSession).toHaveBeenCalledTimes(1));

    await act(async () => {
      await result.current.handleStopStrategicSession();
    });
    expect(stopStrategicSession).toHaveBeenCalledWith('proj-1');
    rerender({
      liveness: {
        ...initial,
        resident_session: {
          ...initial.resident_session,
          status: 'ended',
          stop_requested: true,
          ended_at: new Date().toISOString(),
        },
        resident_session_availability: {
          available: false,
          state: 'off',
          reason: 'Stopped by operator',
        },
      },
    });
    await vi.advanceTimersByTimeAsync(600);
    expect(startStrategicSession).toHaveBeenCalledTimes(1);

    await act(async () => {
      await result.current.handleStartStrategicSession();
    });
    expect(startStrategicSession).toHaveBeenCalledTimes(2);
    expect(startStrategicSession).toHaveBeenLastCalledWith('proj-1', {});
  });

  it('allows Workday auto-ensure again after leaving and explicitly re-entering continuous mode', async () => {
    vi.mocked(startStrategicSession).mockResolvedValue(successfulStartResponse());
    vi.mocked(stopStrategicSession).mockResolvedValue({
      project_id: 'proj-1',
      session: { session_id: 'ss-live', status: 'ended', stop_requested: true },
    });
    const initial = makeLiveness();
    const { result, rerender } = renderHook(
      ({ liveness }: { liveness: StrategicLivenessResponse }) =>
        useStrategicSession({
          selectedProjectId: 'proj-1',
          addToast: vi.fn(),
          strategicReadModel: makeReadModel(null),
          strategicLiveness: liveness,
        }),
      { initialProps: { liveness: initial } },
    );
    await waitFor(() => expect(startStrategicSession).toHaveBeenCalledTimes(1));
    await act(async () => {
      await result.current.handleStopStrategicSession();
    });

    rerender({
      liveness: {
        ...initial,
        interaction_mode: 'chat',
        resident_session: {
          ...initial.resident_session,
          status: 'ended',
          stop_requested: true,
        },
      },
    });
    await vi.advanceTimersByTimeAsync(50);
    expect(startStrategicSession).toHaveBeenCalledTimes(1);

    rerender({
      liveness: {
        ...initial,
        resident_session: {
          ...initial.resident_session,
          status: 'ended',
          stop_requested: true,
        },
      },
    });
    await waitFor(() => expect(startStrategicSession).toHaveBeenCalledTimes(2));
    expect(startStrategicSession).toHaveBeenLastCalledWith('proj-1', {
      expected_interaction_mode: 'continuous',
    });
  });

  it('treats retry_wait as healthy dormant state and does not restart it on reload', async () => {
    const liveness = makeLiveness();
    liveness.resident_session = {
      ...liveness.resident_session,
      worker_state: 'retry_wait',
      worker_retry_until: '2026-06-01T13:00:00Z',
      worker_retry_reason: 'Provider rate limit',
    };
    renderHook(() =>
      useStrategicSession({
        selectedProjectId: 'proj-1',
        addToast: vi.fn(),
        strategicReadModel: makeReadModel(null),
        strategicLiveness: liveness,
      }),
    );

    await Promise.resolve();
    expect(startStrategicSession).not.toHaveBeenCalled();
  });

  it('does not let a retained retry_wait field suppress restart of an ended session', async () => {
    const liveness = makeLiveness();
    liveness.resident_session = {
      ...liveness.resident_session,
      status: 'ended',
      ended_at: '2026-06-01T11:00:00Z',
      worker_state: 'retry_wait',
      worker_retry_until: '2026-06-01T13:00:00Z',
      worker_retry_reason: 'Provider rate limit',
    };
    vi.mocked(startStrategicSession).mockResolvedValue(
      successfulStartResponse(),
    );
    renderHook(() =>
      useStrategicSession({
        selectedProjectId: 'proj-1',
        addToast: vi.fn(),
        strategicReadModel: makeReadModel(null),
        strategicLiveness: liveness,
      }),
    );

    await waitFor(() => expect(startStrategicSession).toHaveBeenCalledTimes(1));
  });

  it('ensures a stale non-retry worker so the backend can replace it', async () => {
    const liveness = makeLiveness();
    liveness.resident_session = {
      ...liveness.resident_session,
      worker_state: 'ready',
      updated_at: '2026-06-01T10:00:00Z',
    };
    vi.mocked(startStrategicSession).mockResolvedValue(
      successfulStartResponse(),
    );
    renderHook(() =>
      useStrategicSession({
        selectedProjectId: 'proj-1',
        addToast: vi.fn(),
        strategicReadModel: makeReadModel(null),
        strategicLiveness: liveness,
      }),
    );

    await waitFor(() => expect(startStrategicSession).toHaveBeenCalledTimes(1));
  });

  it('re-ensures a terminal worker generation on the same page exactly once', async () => {
    vi.mocked(startStrategicSession).mockResolvedValue(
      successfulStartResponse(),
    );
    const readModel = makeReadModel(null);
    const healthy = makeLiveness();
    healthy.resident_session = {
      ...healthy.resident_session,
      worker_generation: 3,
      worker_state: 'ready',
      updated_at: new Date().toISOString(),
    };
    const { rerender } = renderHook(
      ({ liveness }: { liveness: StrategicLivenessResponse }) =>
        useStrategicSession({
          selectedProjectId: 'proj-1',
          addToast: vi.fn(),
          strategicReadModel: readModel,
          strategicLiveness: liveness,
        }),
      { initialProps: { liveness: healthy } },
    );

    await waitFor(() => expect(startStrategicSession).toHaveBeenCalledTimes(1));

    const terminal: StrategicLivenessResponse = {
      ...healthy,
      resident_session: {
        ...healthy.resident_session,
        status: 'ended',
        worker_state: 'restart_failed',
        ended_at: new Date().toISOString(),
      },
      resident_session_availability: {
        available: true,
        state: 'error',
        reason: 'start_failed: worker exited',
      },
    };
    rerender({ liveness: terminal });

    await waitFor(() => expect(startStrategicSession).toHaveBeenCalledTimes(2));
    rerender({ liveness: { ...terminal } });
    await vi.advanceTimersByTimeAsync(50);
    expect(startStrategicSession).toHaveBeenCalledTimes(2);

    const recovered: StrategicLivenessResponse = {
      ...healthy,
      resident_session: {
        ...healthy.resident_session,
        session_id: 'ss-recovered',
        worker_generation: 4,
      },
    };
    rerender({ liveness: recovered });
    await vi.advanceTimersByTimeAsync(50);
    expect(startStrategicSession).toHaveBeenCalledTimes(2);
  });

  it('re-ensures when backend exposes start_failed error before the active blob clears', async () => {
    vi.mocked(startStrategicSession).mockResolvedValue(
      successfulStartResponse(),
    );
    const readModel = makeReadModel(null);
    const healthy = makeLiveness();
    healthy.resident_session = {
      ...healthy.resident_session,
      worker_generation: 9,
      worker_state: 'ready',
      updated_at: new Date().toISOString(),
    };
    const { rerender } = renderHook(
      ({ liveness }: { liveness: StrategicLivenessResponse }) =>
        useStrategicSession({
          selectedProjectId: 'proj-1',
          addToast: vi.fn(),
          strategicReadModel: readModel,
          strategicLiveness: liveness,
        }),
      { initialProps: { liveness: healthy } },
    );
    await waitFor(() => expect(startStrategicSession).toHaveBeenCalledTimes(1));

    const failedButNotYetCleared: StrategicLivenessResponse = {
      ...healthy,
      resident_session: {
        ...healthy.resident_session,
        status: 'active',
        worker_state: 'ready',
      },
      resident_session_availability: {
        available: true,
        state: 'error',
        reason: 'start_failed: terminal worker could not restart',
      },
    };
    rerender({ liveness: failedButNotYetCleared });

    await waitFor(() => expect(startStrategicSession).toHaveBeenCalledTimes(2));
    rerender({ liveness: { ...failedButNotYetCleared } });
    await vi.advanceTimersByTimeAsync(50);
    expect(startStrategicSession).toHaveBeenCalledTimes(2);
  });

  it('re-ensures an observed wedged starting worker without repeating the epoch', async () => {
    vi.mocked(startStrategicSession).mockResolvedValue(
      successfulStartResponse(),
    );
    const readModel = makeReadModel(null);
    const healthy = makeLiveness();
    const { rerender } = renderHook(
      ({ liveness }: { liveness: StrategicLivenessResponse }) =>
        useStrategicSession({
          selectedProjectId: 'proj-1',
          addToast: vi.fn(),
          strategicReadModel: readModel,
          strategicLiveness: liveness,
        }),
      { initialProps: { liveness: healthy } },
    );
    await waitFor(() => expect(startStrategicSession).toHaveBeenCalledTimes(1));

    const wedged: StrategicLivenessResponse = {
      ...healthy,
      resident_session: {
        ...healthy.resident_session,
        worker_generation: 7,
        worker_state: 'starting',
        started_at: '2020-01-01T00:00:00Z',
        updated_at: '2020-01-01T00:00:00Z',
      },
      resident_session_availability: {
        available: true,
        state: 'starting',
        reason: null,
      },
    };
    rerender({ liveness: wedged });

    await waitFor(() => expect(startStrategicSession).toHaveBeenCalledTimes(2));
    rerender({ liveness: { ...wedged } });
    await vi.advanceTimersByTimeAsync(50);
    expect(startStrategicSession).toHaveBeenCalledTimes(2);
  });

  it('wakes itself when an unchanged starting snapshot crosses the stale deadline', async () => {
    vi.mocked(startStrategicSession).mockResolvedValue(
      successfulStartResponse(),
    );
    const readModel = makeReadModel(null);
    const nearDeadline = makeLiveness();
    const startedAt = new Date(Date.now() - 5 * 60_000 + 100).toISOString();
    nearDeadline.resident_session = {
      ...nearDeadline.resident_session,
      worker_generation: 11,
      worker_state: 'starting',
      started_at: startedAt,
      updated_at: startedAt,
    };
    nearDeadline.resident_session_availability = {
      available: true,
      state: 'starting',
      reason: null,
    };

    renderHook(() =>
      useStrategicSession({
        selectedProjectId: 'proj-1',
        addToast: vi.fn(),
        strategicReadModel: readModel,
        strategicLiveness: nearDeadline,
      }),
    );

    await waitFor(() => expect(startStrategicSession).toHaveBeenCalledTimes(1));
    await waitFor(
      () => expect(startStrategicSession).toHaveBeenCalledTimes(2),
      { timeout: 1_000 },
    );
  });

  it('keeps callbacks captured before an operate downgrade from making Strategic writes', async () => {
    const readModel = makeReadModel(makeHeavyState());
    const { result, rerender } = renderHook(
      ({ canOperateProject }: { canOperateProject: boolean }) =>
        useStrategicSession({
          selectedProjectId: 'proj-1',
          canOperateProject,
          addToast: vi.fn(),
          strategicReadModel: readModel,
          strategicLiveness: null,
        }),
      { initialProps: { canOperateProject: true } },
    );
    const staleCallbacks = {
      persist: result.current.persistStrategicWorkbench,
      updateRefs: result.current.updateStrategicSessionRefs,
      selectRunner: result.current.handleSelectStrategicRunner,
      checkIn: result.current.handleStrategicCheckIn,
      start: result.current.handleStartStrategicSession,
      stop: result.current.handleStopStrategicSession,
    };

    rerender({ canOperateProject: false });
    vi.clearAllMocks();
    await act(async () => {
      staleCallbacks.persist({ interaction_mode: 'continuous' });
      staleCallbacks.updateRefs(refs => refs);
      staleCallbacks.selectRunner('runner-after-revocation');
      await staleCallbacks.checkIn();
      await staleCallbacks.start();
      await staleCallbacks.stop();
      await Promise.resolve();
    });

    expect(updateStrategicWorkbenchState).not.toHaveBeenCalled();
    expect(checkInStrategicSession).not.toHaveBeenCalled();
    expect(startStrategicSession).not.toHaveBeenCalled();
    expect(stopStrategicSession).not.toHaveBeenCalled();
    expect(readModel.refresh).not.toHaveBeenCalled();
  });
});
