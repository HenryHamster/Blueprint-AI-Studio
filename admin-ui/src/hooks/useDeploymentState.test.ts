import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, cleanup } from '@testing-library/react';
import { useDeploymentState } from './useDeploymentState';
import { authGet } from '../api';

vi.mock('../api', () => ({
  authHeaders: vi.fn().mockResolvedValue({ Authorization: 'Bearer ***', 'Content-Type': 'application/json' }),
  authGet: vi.fn(),
  API_BASE_URL: '',
}));

const mockAuthGet = vi.mocked(authGet);

function makeUser() {
  return { uid: 'user-1', email: 'test@example.com' } as Parameters<typeof useDeploymentState>[1];
}

function okGet(data: unknown) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve(data) } as Response);
}

function stubAllEndpoints(overrides: Record<string, unknown> = {}) {
  mockAuthGet.mockImplementation((url: string) => {
    const match = Object.entries(overrides).find(([key]) => url.includes(key));
    return match ? okGet(match[1]) : okGet({});
  });
}

let fetchSpy: ReturnType<typeof vi.fn>;

describe('useDeploymentState', () => {
  const originalError = console.error;

  beforeEach(() => {
    // Unmount any hooks from previous tests to prevent state-update leakage
    // that would cause act() to hang waiting for pending React work.
    cleanup();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    console.error = vi.fn();
    fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({}),
    });
    vi.stubGlobal('fetch', fetchSpy);
    stubAllEndpoints();
  });

  afterEach(() => {
    console.error = originalError;
    // Explicitly unmount hooks and clear mocks so previous test state can't
    // bleed into the next test via floating Promises or lingering intervals.
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  // --- Initial state ---

  it('returns null for all state fields when user is null', () => {
    const { result } = renderHook(() =>
      useDeploymentState(vi.fn(), null, { current: vi.fn() }),
    );
    expect(result.current.deployState).toBeNull();
    expect(result.current.zoneHealth).toBeNull();
    expect(result.current.orchestratorHealth).toBeNull();
    expect(result.current.detailedHealth).toBeNull();
    expect(result.current.diagnostics).toBeNull();
  });

  it('does not call authGet when user is null', async () => {
    renderHook(() => useDeploymentState(vi.fn(), null, { current: vi.fn() }));
    // Brief wait to ensure no deferred effects fire
    await vi.advanceTimersByTimeAsync(20);
    expect(mockAuthGet).not.toHaveBeenCalled();
  });

  it('defers health requests until runtime polling is enabled', async () => {
    const { rerender } = renderHook(
      ({ enabled }) => useDeploymentState(vi.fn(), makeUser(), { current: vi.fn() }, 'proj-123', enabled),
      { initialProps: { enabled: false } },
    );
    await vi.advanceTimersByTimeAsync(20);
    expect(mockAuthGet).not.toHaveBeenCalled();

    rerender({ enabled: true });
    await waitFor(() => {
      expect(mockAuthGet).toHaveBeenCalledWith(expect.stringContaining('/api/admin/deploy-state'));
      expect(mockAuthGet).toHaveBeenCalledWith(expect.stringContaining('/api/admin/zone-health'));
      expect(mockAuthGet).toHaveBeenCalledWith(expect.stringContaining('/api/debug/orchestrator'));
      expect(mockAuthGet).toHaveBeenCalledWith(expect.stringContaining('/api/health/detailed'));
      expect(mockAuthGet).toHaveBeenCalledWith(expect.stringContaining('/api/admin/projects/proj-123/diagnostics'));
    });
  });

  // --- Health polling on mount ---

  it('fetches all four health endpoints on mount when user is provided', async () => {
    renderHook(() => useDeploymentState(vi.fn(), makeUser(), { current: vi.fn() }));
    await waitFor(() => {
      expect(mockAuthGet).toHaveBeenCalledWith(expect.stringContaining('/api/admin/deploy-state'));
      expect(mockAuthGet).toHaveBeenCalledWith(expect.stringContaining('/api/admin/zone-health'));
      expect(mockAuthGet).toHaveBeenCalledWith(expect.stringContaining('/api/debug/orchestrator'));
      expect(mockAuthGet).toHaveBeenCalledWith(expect.stringContaining('/api/health/detailed'));
    });
  });

  it('sets deployState when fetch returns data', async () => {
    const deployData = { status: 'HEALTHY', rollback_count: 0, rollback_reason: null };
    stubAllEndpoints({ '/api/admin/deploy-state': deployData });
    const { result } = renderHook(() =>
      useDeploymentState(vi.fn(), makeUser(), { current: vi.fn() }),
    );
    await waitFor(() => {
      expect(result.current.deployState).toEqual(deployData);
    });
  });

  it('fires error toast when deploy state fetch throws', async () => {
    mockAuthGet.mockImplementation((url: string) => {
      if (url.includes('/api/admin/deploy-state')) throw new Error('Network error');
      return okGet({});
    });
    const addToast = vi.fn();
    renderHook(() => useDeploymentState(addToast, makeUser(), { current: vi.fn() }));
    await waitFor(() => {
      expect(addToast).toHaveBeenCalledWith('Failed to load deploy state', 'error');
    });
  });

  // --- Orchestrator health ---

  it('sets orchestratorHealth from /api/debug/orchestrator response', async () => {
    const health = { last_cycle_seconds_ago: 5, queued_tasks: 2, active_tasks: 1, pool_info: {} };
    stubAllEndpoints({ '/api/debug/orchestrator': health });
    const { result } = renderHook(() =>
      useDeploymentState(vi.fn(), makeUser(), { current: vi.fn() }),
    );
    await waitFor(() => {
      expect(result.current.orchestratorHealth?.queued_tasks).toBe(2);
      expect(result.current.orchestratorHealth?.active_tasks).toBe(1);
    });
  });

  it('passes through progress-based liveness fields onto orchestratorHealth', async () => {
    const health = {
      last_cycle_seconds_ago: 600,
      queued_tasks: 2,
      active_tasks: 1,
      pool_info: {},
      orchestrator_alive: true,
      orchestrator_hung: false,
      orchestrator_progress_age_seconds: 4,
      orchestrator_cycle_age_seconds: 600,
      orchestrator_liveness_reason: 'progressing',
    };
    stubAllEndpoints({ '/api/debug/orchestrator': health });
    const { result } = renderHook(() =>
      useDeploymentState(vi.fn(), makeUser(), { current: vi.fn() }),
    );
    await waitFor(() => {
      // A slow-but-progressing cycle: stale cycle age, fresh progress heartbeat.
      expect(result.current.orchestratorHealth?.orchestrator_alive).toBe(true);
      expect(result.current.orchestratorHealth?.orchestrator_hung).toBe(false);
      expect(result.current.orchestratorHealth?.orchestrator_progress_age_seconds).toBe(4);
      expect(result.current.orchestratorHealth?.orchestrator_liveness_reason).toBe('progressing');
    });
  });

  it('sets orchestratorHealth to null when /api/debug/orchestrator returns non-ok', async () => {
    mockAuthGet.mockImplementation((url: string) => {
      if (url.includes('/api/debug/orchestrator')) {
        return Promise.resolve({ ok: false, status: 503 } as Response);
      }
      return okGet({});
    });
    const { result } = renderHook(() =>
      useDeploymentState(vi.fn(), makeUser(), { current: vi.fn() }),
    );
    await waitFor(() => {
      // At least one poll has run: orchestratorHealth should be set to null
      expect(result.current.orchestratorHealth).toBeNull();
      // And the other effects have completed (deployState set from empty response)
      expect(result.current.deployState).not.toBeNull();
    });
  });

  // --- Detailed health / circuit breaker parsing ---

  it('parses circuit breaker state from detailed health response', async () => {
    const detailedData = {
      components: {
        circuit_breaker: {
          projects: {
            proj1: { state: 'OPEN', failure_count: 5, opened_at: '2024-01-01T00:00:00Z' },
          },
        },
        max_plan_pool: { total: 10, available: 3 },
        autonomous: { enabled: true, consecutive_failures: 0 },
      },
    };
    stubAllEndpoints({ '/api/health/detailed': detailedData });
    const { result } = renderHook(() =>
      useDeploymentState(vi.fn(), makeUser(), { current: vi.fn() }),
    );
    await waitFor(() => {
      expect(result.current.detailedHealth?.circuit_breaker.projects['proj1'].state).toBe('OPEN');
      expect(result.current.detailedHealth?.max_plan_pool.total).toBe(10);
      expect(result.current.detailedHealth?.autonomous.enabled).toBe(true);
    });
  });

  it('sets detailedHealth to null when /api/health/detailed returns non-ok', async () => {
    mockAuthGet.mockImplementation((url: string) => {
      if (url.includes('/api/health/detailed')) {
        return Promise.resolve({ ok: false, status: 500 } as Response);
      }
      return okGet({});
    });
    const { result } = renderHook(() =>
      useDeploymentState(vi.fn(), makeUser(), { current: vi.fn() }),
    );
    await waitFor(() => {
      expect(result.current.detailedHealth).toBeNull();
      // Other endpoints still run; deployState should be set
      expect(result.current.deployState).not.toBeNull();
    });
  });

  // --- Diagnostics ---

  it('fetches diagnostics when selectedProjectId is provided', async () => {
    const diagnosticsData = { tasks: [], directions: [] };
    stubAllEndpoints({ '/diagnostics': diagnosticsData });
    const { result } = renderHook(() =>
      useDeploymentState(vi.fn(), makeUser(), { current: vi.fn() }, 'proj-123'),
    );
    await waitFor(() => {
      expect(mockAuthGet).toHaveBeenCalledWith(
        expect.stringContaining('/api/admin/projects/proj-123/diagnostics'),
      );
      expect(result.current.diagnostics).toEqual(diagnosticsData);
    });
  });

  it('does not fetch diagnostics when selectedProjectId is null', async () => {
    const { result } = renderHook(() =>
      useDeploymentState(vi.fn(), makeUser(), { current: vi.fn() }, null),
    );
    // Wait for the standard health effects to settle (they set deployState, etc.)
    await waitFor(() => expect(result.current.deployState).not.toBeNull());
    // With no selectedProjectId the effect bails out immediately, leaving
    // diagnostics null (if it were fetched the mock would return {} not null).
    expect(result.current.diagnostics).toBeNull();
  });

  // --- Action handlers ---
  //
  // Handlers are async functions. After awaiting them, synchronous side effects
  // (addToast, fetchTasksRef.current calls) are already done — assert without act.
  // The floating re-fetch (fetchDeployState / fetchTasksRef) triggers a state update
  // outside act; React warns but we suppress via console.error = vi.fn().

  it('handlePausePipeline posts to pause endpoint and shows success toast', async () => {
    const addToast = vi.fn();
    const { result } = renderHook(() =>
      useDeploymentState(addToast, makeUser(), { current: vi.fn() }),
    );
    await result.current.handlePausePipeline();
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.stringContaining('/api/admin/deploy-state/pause'),
      expect.objectContaining({ method: 'POST' }),
    );
    expect(addToast).toHaveBeenCalledWith('Pipeline paused', 'success');
  });

  it('handlePausePipeline shows error toast on failure', async () => {
    fetchSpy.mockResolvedValue({ ok: false, statusText: 'Forbidden' });
    const addToast = vi.fn();
    const { result } = renderHook(() =>
      useDeploymentState(addToast, makeUser(), { current: vi.fn() }),
    );
    await result.current.handlePausePipeline();
    expect(addToast).toHaveBeenCalledWith('Failed to pause pipeline', 'error');
  });

  it('handleResumePipeline posts to resume endpoint and calls fetchTasksRef', async () => {
    const addToast = vi.fn();
    const fetchTasksRef = { current: vi.fn() };
    const { result } = renderHook(() =>
      useDeploymentState(addToast, makeUser(), fetchTasksRef),
    );
    await result.current.handleResumePipeline();
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.stringContaining('/api/admin/deploy-state/resume'),
      expect.objectContaining({ method: 'POST' }),
    );
    expect(addToast).toHaveBeenCalledWith('Pipeline resumed', 'success');
    expect(fetchTasksRef.current).toHaveBeenCalled();
  });

  it('handleRollback shows toast with rolled-back SHA on success', async () => {
    fetchSpy.mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({ from_sha: 'abcdef1234567890', to_sha: '98765432abcdef' }),
    });
    const addToast = vi.fn();
    const { result } = renderHook(() =>
      useDeploymentState(addToast, makeUser(), { current: vi.fn() }),
    );
    await result.current.handleRollback();
    expect(addToast).toHaveBeenCalledWith(
      expect.stringContaining('abcdef12'),
      'success',
    );
  });

  it('handleRollback treats a 200 rollback_failed body as a retryable error, not success', async () => {
    // Backend returns HTTP 200 with status='rollback_failed' when the Cloud Run
    // traffic shift never happened (it avoids a 5xx that would quarantine the
    // serving instance). The hook must surface this as an error/retry toast and
    // must NOT show the rolled-back success toast.
    fetchSpy.mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({
        status: 'rollback_failed',
        gcloud_error: 'traffic shift rejected',
        serving_sha: 'sha123bad000000',
        from_sha: 'sha123bad000000',
        to_sha: 'sha123good00000',
      }),
    });
    const addToast = vi.fn();
    const { result } = renderHook(() =>
      useDeploymentState(addToast, makeUser(), { current: vi.fn() }),
    );
    await result.current.handleRollback();
    expect(addToast).toHaveBeenCalledWith(
      expect.stringContaining('Rollback failed'),
      'error',
    );
    // The error toast surfaces the still-serving SHA and the gcloud reason.
    expect(addToast).toHaveBeenCalledWith(
      expect.stringContaining('sha123ba'),
      'error',
    );
    expect(addToast).toHaveBeenCalledWith(
      expect.stringContaining('traffic shift rejected'),
      'error',
    );
    // Crucially, no success toast was fired for the failed shift.
    expect(addToast).not.toHaveBeenCalledWith(
      expect.stringContaining('Rolled back:'),
      'success',
    );
  });

  it('handleRollback shows success toast on rolled_back status body', async () => {
    // Positive companion to the rollback_failed test: a status='rolled_back'
    // body (real traffic shift) still fires the success toast — guards against
    // the failure branch matching too broadly.
    fetchSpy.mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({
        status: 'rolled_back',
        from_sha: 'abcdef1234567890',
        to_sha: '98765432abcdef',
      }),
    });
    const addToast = vi.fn();
    const { result } = renderHook(() =>
      useDeploymentState(addToast, makeUser(), { current: vi.fn() }),
    );
    await result.current.handleRollback();
    expect(addToast).toHaveBeenCalledWith(
      expect.stringContaining('abcdef12'),
      'success',
    );
    expect(addToast).not.toHaveBeenCalledWith(
      expect.anything(),
      'error',
    );
  });

  it('handleRollback ignores a second concurrent call while in flight', async () => {
    // Gate the first fetch on a promise we control so the call is still
    // in flight when the second handleRollback fires.
    let releaseFirst: () => void = () => {};
    const firstResponse = {
      ok: true,
      json: vi.fn().mockResolvedValue({ from_sha: 'abcdef1234567890', to_sha: '98765432abcdef' }),
    };
    fetchSpy.mockImplementationOnce(
      () => new Promise((resolve) => { releaseFirst = () => resolve(firstResponse); }),
    );
    const addToast = vi.fn();
    const { result } = renderHook(() =>
      useDeploymentState(addToast, makeUser(), { current: vi.fn() }),
    );

    const firstCall = result.current.handleRollback();
    // Second call while the first is still pending — must be a no-op.
    await result.current.handleRollback();
    expect(fetchSpy).toHaveBeenCalledTimes(1);

    releaseFirst();
    await firstCall;
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it('handleRollback shows error toast on failure', async () => {
    fetchSpy.mockResolvedValue({
      ok: false,
      statusText: 'Internal Server Error',
      json: vi.fn().mockResolvedValue({ detail: 'No stable SHA' }),
    });
    const addToast = vi.fn();
    const { result } = renderHook(() =>
      useDeploymentState(addToast, makeUser(), { current: vi.fn() }),
    );
    await result.current.handleRollback();
    expect(addToast).toHaveBeenCalledWith(
      expect.stringContaining('Rollback failed'),
      'error',
    );
  });

  // --- Durable failed-rollback affordance ---
  //
  // The error toast fired on a failed traffic shift auto-dismisses after a few
  // seconds (Toasts.tsx). The hook promotes that ephemeral signal into standing
  // `failedRollback` state so the deploy surface can render a durable affordance
  // that outlives the toast and clears on the operator's next resolving action.

  // These tests mirror the existing handler-test style (await the async handler
  // directly; the floating re-fetch triggers a state update outside act, which
  // React warns about — suppressed via console.error = vi.fn() in beforeEach).
  // We deliberately do NOT wrap handlers in act(): act() forces React to re-flush
  // until the tree is stable, and combined with a deploy-state mock that returns
  // a fresh object every poll it spins into a "Maximum update depth" loop in this
  // hook's multi-poller setup — a test-harness artifact, not a code defect.

  it('sets failedRollback from a 200 rollback_failed body and keeps it past the toast window', async () => {
    stubAllEndpoints({ '/api/admin/deploy-state': { status: 'STABLE' } });
    const { result } = renderHook(() =>
      useDeploymentState(vi.fn(), makeUser(), { current: vi.fn() }),
    );
    await waitFor(() => expect(result.current.deployState?.status).toBe('STABLE'));

    // Rollback whose Cloud Run traffic shift fails: HTTP 200 rollback_failed.
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      json: vi.fn().mockResolvedValue({
        status: 'rollback_failed',
        serving_sha: 'sha123bad000000',
        gcloud_error: 'traffic shift rejected',
      }),
    });
    await result.current.handleRollback();

    // Standing state reconstructed from the response — decoupled from the toast.
    await waitFor(() => {
      expect(result.current.failedRollback).toEqual({
        serving_sha: 'sha123bad000000',
        gcloud_error: 'traffic shift rejected',
      });
    });

    // The toast auto-dismisses after 4000ms (Toasts.tsx); the affordance must
    // outlive it. The hook holds it as state with no self-clearing timer, so it
    // is still present well past that window.
    await vi.advanceTimersByTimeAsync(4100);
    expect(result.current.failedRollback?.serving_sha).toBe('sha123bad000000');
  });

  it('clears failedRollback on a subsequent successful rollback', async () => {
    stubAllEndpoints({ '/api/admin/deploy-state': { status: 'STABLE' } });
    const { result } = renderHook(() =>
      useDeploymentState(vi.fn(), makeUser(), { current: vi.fn() }),
    );
    await waitFor(() => expect(result.current.deployState?.status).toBe('STABLE'));

    fetchSpy.mockResolvedValueOnce({
      ok: true,
      json: vi.fn().mockResolvedValue({ status: 'rollback_failed', serving_sha: 'sha123bad000000' }),
    });
    await result.current.handleRollback();
    await waitFor(() => expect(result.current.failedRollback?.serving_sha).toBe('sha123bad000000'));

    // A real traffic shift (status='rolled_back') resolves the situation.
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      json: vi.fn().mockResolvedValue({ status: 'rolled_back', from_sha: 'sha123bad000000', to_sha: 'sha999good00000' }),
    });
    await result.current.handleRollback();
    await waitFor(() => expect(result.current.failedRollback).toBeNull());
  });

  it('clears failedRollback when the operator marks the deploy stable', async () => {
    stubAllEndpoints({ '/api/admin/deploy-state': { status: 'STABLE' } });
    const { result } = renderHook(() =>
      useDeploymentState(vi.fn(), makeUser(), { current: vi.fn() }),
    );
    await waitFor(() => expect(result.current.deployState?.status).toBe('STABLE'));

    fetchSpy.mockResolvedValueOnce({
      ok: true,
      json: vi.fn().mockResolvedValue({ status: 'rollback_failed', serving_sha: 'sha123bad000000' }),
    });
    await result.current.handleRollback();
    await waitFor(() => expect(result.current.failedRollback).not.toBeNull());

    await result.current.handleMarkStable();
    await waitFor(() => expect(result.current.failedRollback).toBeNull());
  });

  it('auto-clears failedRollback when deploy-state status moves away from the at-failure status', async () => {
    stubAllEndpoints({ '/api/admin/deploy-state': { status: 'STABLE' } });
    const { result } = renderHook(() =>
      useDeploymentState(vi.fn(), makeUser(), { current: vi.fn() }),
    );
    await waitFor(() => expect(result.current.deployState?.status).toBe('STABLE'));

    fetchSpy.mockResolvedValueOnce({
      ok: true,
      json: vi.fn().mockResolvedValue({ status: 'rollback_failed', serving_sha: 'sha123bad000000' }),
    });
    await result.current.handleRollback();
    await waitFor(() => expect(result.current.failedRollback).not.toBeNull());

    // An external/operator change moves the deploy status; the next refetch
    // observes it and the standing affordance auto-clears (no false alarm).
    stubAllEndpoints({ '/api/admin/deploy-state': { status: 'PAUSED' } });
    await result.current.handlePausePipeline();
    await waitFor(() => expect(result.current.deployState?.status).toBe('PAUSED'));
    await waitFor(() => expect(result.current.failedRollback).toBeNull());
  });

  it('handleMarkStable posts to mark-stable endpoint and shows success toast', async () => {
    const addToast = vi.fn();
    const { result } = renderHook(() =>
      useDeploymentState(addToast, makeUser(), { current: vi.fn() }),
    );
    await result.current.handleMarkStable();
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.stringContaining('/api/admin/deploy-state/mark-stable'),
      expect.objectContaining({ method: 'POST' }),
    );
    expect(addToast).toHaveBeenCalledWith('Deploy marked stable', 'success');
  });

  it('handleRepairState posts to repair-state and shows count in toast', async () => {
    fetchSpy.mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({ count: 3 }),
    });
    const addToast = vi.fn();
    const fetchTasksRef = { current: vi.fn() };
    const { result } = renderHook(() =>
      useDeploymentState(addToast, makeUser(), fetchTasksRef),
    );
    await result.current.handleRepairState();
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.stringContaining('/api/admin/repair-state'),
      expect.objectContaining({ method: 'POST' }),
    );
    expect(addToast).toHaveBeenCalledWith('Repaired 3 issues', 'success');
    expect(fetchTasksRef.current).toHaveBeenCalled();
  });
});
