import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, cleanup } from '@testing-library/react';
import { useStrategicReadModel } from './useStrategicReadModel';
import { fetchStrategicStateWithRevision } from '../strategicStateApi';
import type { StrategicSessionResponse, StrategicStateResponse } from '../strategicStateApi';

vi.mock('../strategicStateApi', () => ({
  fetchStrategicStateWithRevision: vi.fn(),
  fetchStrategicWorkbenchState: vi.fn(),
  updateStrategicWorkbenchState: vi.fn(),
  fetchStrategicSession: vi.fn(),
  fetchStrategicConversation: vi.fn(),
  createStrategicSessionMessage: vi.fn(),
  checkInStrategicSession: vi.fn(),
  executeStrategicWorkSessionCommand: vi.fn(),
  startStrategicSession: vi.fn(),
  stopStrategicSession: vi.fn(),
  getMessageAttachmentUploadUrl: vi.fn(),
  fetchStrategicState: vi.fn(),
}));

// Disable the poller so tests drive fetches via result.current.refresh().
// usePoller's own behavior is tested in usePoller.test.ts.
vi.mock('./usePoller', () => ({
  DEFAULT_POLL_MS: 30_000,
  usePoller: vi.fn(),
  __pollerInternals: { reset: vi.fn(), hasActiveTimers: vi.fn(), subscriberCount: vi.fn() },
}));

const mockFetch = vi.mocked(fetchStrategicStateWithRevision);

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function makeState(projectId = 'proj-1'): StrategicStateResponse {
  return {
    project_id: projectId,
    strategic: {
      active_cycle: null,
      active_task: null,
      active_phase: null,
      latest_cycle: null,
      refresh: { pending: null },
    },
    running_update: { excerpt: null, char_count: 0, acknowledged_at: null },
    problem_model: {
      active_gap_count: 0,
      capability_count: 0,
      pending_confirmation_count: 0,
      top_gap: null,
    },
    directions: { total_count: 0, active_count: 0, paused_count: 0, primary: null },
    messages: { counts: { open: 0, linked: 0 }, items: [] },
    tasks: {
      counts: { queued: 0, running: 0, evaluating: 0, blocked: 0, failed: 0, completed: 0, total: 0 },
      cumulative_estimated_cost_usd: 0,
    },
    design: { has_design_doc: false },
  };
}

describe('useStrategicReadModel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it('returns null data, false loading, null error when projectId is null', () => {
    const { result } = renderHook(() => useStrategicReadModel(null));
    expect(result.current.data).toBeNull();
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it('sets loading=true on mount when projectId is provided', () => {
    // Never-resolving fetch so we can observe the loading state before it settles
    mockFetch.mockReturnValue(new Promise(() => {}));
    const { result } = renderHook(() => useStrategicReadModel('proj-1'));
    expect(result.current.loading).toBe(true);
  });

  it('resets state on project change and swallows AbortError', async () => {
    // First, load data for proj-1
    mockFetch.mockResolvedValueOnce(makeState('proj-1'));
    const { result, rerender } = renderHook(
      ({ projectId }: { projectId: string | null }) => useStrategicReadModel(projectId),
      { initialProps: { projectId: 'proj-1' as string | null } },
    );
    await act(async () => { await result.current.refresh(); });
    expect(result.current.data?.project_id).toBe('proj-1');
    expect(result.current.error).toBeNull();

    // Switch to proj-2 — state must reset immediately (before any fetch)
    act(() => { rerender({ projectId: 'proj-2' }); });
    expect(result.current.data).toBeNull();
    expect(result.current.error).toBeNull();
    expect(result.current.loading).toBe(true);

    // An AbortError thrown by the in-flight fetch must be swallowed, not stored
    const abortErr = new Error('The operation was aborted');
    abortErr.name = 'AbortError';
    mockFetch.mockRejectedValueOnce(abortErr);
    await act(async () => { await result.current.refresh(); });
    expect(result.current.error).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it('populates data and clears loading/error on successful fetch', async () => {
    const state = makeState();
    mockFetch.mockResolvedValueOnce(state);
    const { result } = renderHook(() => useStrategicReadModel('proj-1'));
    await act(async () => { await result.current.refresh(); });
    expect(result.current.data).toEqual(state);
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it('ignores a stale response when the selected project changes before fetch resolves', async () => {
    const slowProjectOne = deferred<StrategicStateResponse>();
    mockFetch.mockReturnValueOnce(slowProjectOne.promise);
    const { result, rerender } = renderHook(
      ({ projectId }: { projectId: string | null }) => useStrategicReadModel(projectId),
      { initialProps: { projectId: 'proj-1' as string | null } },
    );

    let refreshPromise: Promise<void> = Promise.resolve();
    await act(async () => {
      refreshPromise = result.current.refresh();
      await Promise.resolve();
    });
    act(() => { rerender({ projectId: 'proj-2' }); });

    await act(async () => {
      slowProjectOne.resolve(makeState('proj-1'));
      await refreshPromise;
    });

    expect(result.current.data).toBeNull();
    expect(result.current.error).toBeNull();
    expect(result.current.loading).toBe(true);
  });

  it('sets error message and clears loading when fetch fails with non-abort error', async () => {
    mockFetch.mockRejectedValueOnce(new Error('Network error'));
    const { result } = renderHook(() => useStrategicReadModel('proj-1'));
    await act(async () => { await result.current.refresh(); });
    expect(result.current.error).toBe('Network error');
    expect(result.current.loading).toBe(false);
    expect(result.current.data).toBeNull();
  });

  it('uses fallback message for non-Error rejections', async () => {
    mockFetch.mockRejectedValueOnce('string-error');
    const { result } = renderHook(() => useStrategicReadModel('proj-1'));
    await act(async () => { await result.current.refresh(); });
    expect(result.current.error).toBe('Failed to fetch strategic state');
  });

  it('stale-while-revalidate: loading stays false during a poll when data already exists', async () => {
    const state = makeState();
    mockFetch.mockResolvedValue(state);
    const { result } = renderHook(() => useStrategicReadModel('proj-1'));

    // First fetch populates data
    await act(async () => { await result.current.refresh(); });
    expect(result.current.data).not.toBeNull();

    // On a subsequent poll, loading must remain false (data exists → show stale data)
    let loadingMidPoll: boolean | undefined;
    mockFetch.mockImplementationOnce(async () => {
      loadingMidPoll = result.current.loading;
      return state;
    });
    await act(async () => { await result.current.refresh(); });
    expect(loadingMidPoll).toBe(false);
  });

  it('merges Strategic session responses into the current read model', async () => {
    const state = makeState();
    mockFetch.mockResolvedValueOnce(state);
    const { result } = renderHook(() => useStrategicReadModel('proj-1'));
    await act(async () => { await result.current.refresh(); });

    const sessionResponse: StrategicSessionResponse = {
      project_id: 'proj-1',
      status: 'idle',
      location: 'locally',
      session: {
        version: 1,
        id: 'strategic-session:proj-1',
        mode: 'chat',
        active_surface: 'strategic',
        selected_refs: [],
        selected_ref_descriptors: [],
        curated_metrics: [],
        last_check_in: {
          message_id: 'msg-check',
          created_at: '2026-06-30T12:00:00Z',
          kind: 'manual',
        },
        since_last_check_in: {
          version: 1,
          available: true,
          since: '2026-06-30T12:00:00Z',
          until: '2026-06-30T12:00:01Z',
          messages: { total: 0 },
          tasks: { created: 0, updated: 0 },
          alerts: { raised: 0 },
          strategic: { cycles_completed: 0 },
        },
      },
      workbench: null,
      strategic: state.strategic,
      runtime: { local_runner: null, work: null },
      context_package: null,
      conversation: { limit: 20, items: [] },
      recent_outputs: [],
    };

    act(() => {
      result.current.applySessionResponse(sessionResponse);
    });

    expect(result.current.data?.work_session?.last_check_in?.message_id).toBe('msg-check');
    expect(result.current.data?.work_session?.since_last_check_in?.available).toBe(true);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it('drops an in-flight refresh that resolves after a session mutation was applied', async () => {
    // Prime the read model.
    mockFetch.mockResolvedValueOnce(makeState());
    const { result } = renderHook(() => useStrategicReadModel('proj-1'));
    await act(async () => { await result.current.refresh(); });

    // A poll GET starts and is still in flight — it captured a pre-mutation snapshot.
    const inflightPoll = deferred<StrategicStateResponse>();
    mockFetch.mockReturnValueOnce(inflightPoll.promise);
    let pollPromise: Promise<void> = Promise.resolve();
    await act(async () => {
      pollPromise = result.current.refresh();
      await Promise.resolve();
    });

    // A mutation merges fresh session state while that poll is still in flight.
    const sessionResponse: StrategicSessionResponse = {
      project_id: 'proj-1',
      status: 'idle',
      location: 'locally',
      session: {
        version: 1,
        id: 'strategic-session:proj-1',
        mode: 'chat',
        active_surface: 'strategic',
        selected_refs: [],
        selected_ref_descriptors: [],
        curated_metrics: [],
        last_check_in: { message_id: 'msg-fresh', created_at: '2026-06-30T12:00:00Z', kind: 'manual' },
        since_last_check_in: {
          version: 1,
          available: true,
          since: '2026-06-30T12:00:00Z',
          until: '2026-06-30T12:00:01Z',
          messages: { total: 0 },
          tasks: { created: 0, updated: 0 },
          alerts: { raised: 0 },
          strategic: { cycles_completed: 0 },
        },
      },
      workbench: null,
      strategic: makeState().strategic,
      runtime: { local_runner: null, work: null },
      context_package: null,
      conversation: { limit: 20, items: [] },
      recent_outputs: [],
    };
    act(() => { result.current.applySessionResponse(sessionResponse); });
    expect(result.current.data?.work_session?.last_check_in?.message_id).toBe('msg-fresh');

    // The stale poll now resolves with a pre-mutation snapshot (no work_session).
    await act(async () => {
      inflightPoll.resolve(makeState('proj-1'));
      await pollPromise;
    });

    // The freshly merged session state must survive — the stale response is dropped.
    expect(result.current.data?.work_session?.last_check_in?.message_id).toBe('msg-fresh');
  });

  it('skips fetch when signal is already aborted before the call starts', async () => {
    const { result } = renderHook(() => useStrategicReadModel('proj-1'));
    const controller = new AbortController();
    controller.abort();
    await act(async () => { await result.current.refresh(controller.signal); });
    expect(mockFetch).not.toHaveBeenCalled();
    // State stays as set by the initial loading effect — not errored
    expect(result.current.error).toBeNull();
  });

  it('stores state_revision from response and passes it on the next poll', async () => {
    const state1 = { ...makeState('proj-1'), state_revision: 'abc123' };
    const state2 = { ...makeState('proj-1'), state_revision: 'def456' };

    mockFetch.mockResolvedValueOnce(state1);
    mockFetch.mockResolvedValueOnce(state2);

    const { result } = renderHook(() => useStrategicReadModel('proj-1'));

    // First call: no stored revision yet → passes null
    await act(async () => { await result.current.refresh(); });
    expect(mockFetch.mock.calls[0][1]).toBeNull();

    // Second call: should pass the revision from the first response
    await act(async () => { await result.current.refresh(); });
    expect(mockFetch.mock.calls[1][1]).toBe('abc123');
  });

  it('resets state_revision ref on project change', async () => {
    const state1 = { ...makeState('proj-1'), state_revision: 'abc123' };
    const state2 = { ...makeState('proj-2'), state_revision: 'ghi789' };

    mockFetch.mockResolvedValueOnce(state1);
    mockFetch.mockResolvedValueOnce(state2);

    const { result, rerender } = renderHook(
      ({ projectId }: { projectId: string | null }) => useStrategicReadModel(projectId),
      { initialProps: { projectId: 'proj-1' as string | null } },
    );

    // First refresh stores revision
    await act(async () => { await result.current.refresh(); });
    expect(mockFetch.mock.calls[0][1]).toBeNull();

    // Change project — the revision ref must reset
    mockFetch.mockResolvedValueOnce(state2);
    act(() => { rerender({ projectId: 'proj-2' }); });
    await act(async () => { await result.current.refresh(); });

    // The fetch for the new project should pass null (revision reset)
    expect(mockFetch.mock.calls[1][1]).toBeNull();
  });

  it('sets state_revision ref to null when response lacks state_revision', async () => {
    const state1 = { ...makeState('proj-1') };
    // state_revision deliberately absent from the response
    const state2 = { ...makeState('proj-1'), state_revision: 'abc123' };

    mockFetch.mockResolvedValueOnce(state1);
    mockFetch.mockResolvedValueOnce(state2);

    const { result } = renderHook(() => useStrategicReadModel('proj-1'));

    // First call: no stored revision → passes null
    await act(async () => { await result.current.refresh(); });
    expect(mockFetch.mock.calls[0][1]).toBeNull();

    // Second call: revision ref is null because response lacked state_revision
    await act(async () => { await result.current.refresh(); });
    expect(mockFetch.mock.calls[1][1]).toBeNull();
  });
});
