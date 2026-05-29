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
    await new Promise(r => setTimeout(r, 20));
    expect(mockAuthGet).not.toHaveBeenCalled();
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
      expect(result.current.deployState).toBeDefined();
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
      expect(result.current.deployState).toBeDefined();
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
    await waitFor(() => expect(result.current.deployState).toBeDefined());
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
