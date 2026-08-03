import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, test, vi } from 'vitest';
import { useDirectionsData } from './useDirectionsData';

const configSpies = vi.hoisted(() => ({
  fetchCooldownSetting: vi.fn(),
  fetchSettings: vi.fn(),
}));

const apiSpies = vi.hoisted(() => ({
  apiFetch: vi.fn(),
  fetchVersionLifecycle: vi.fn(),
  submitChurnRetirementDecision: vi.fn(),
}));

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    apiFetch: apiSpies.apiFetch,
    fetchVersionLifecycle: apiSpies.fetchVersionLifecycle,
    submitChurnRetirementDecision: apiSpies.submitChurnRetirementDecision,
  });
});

vi.mock('./useAutonomousConfig', () => ({
  useAutonomousConfig: () => ({
    fetchCooldownSetting: configSpies.fetchCooldownSetting,
    fetchSettings: configSpies.fetchSettings,
    cooldownMinutes: 240,
  }),
}));

describe('useDirectionsData read-only boundary', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiSpies.fetchVersionLifecycle.mockResolvedValue(null);
    apiSpies.apiFetch.mockImplementation(async (url: string) => new Response(
      JSON.stringify(url.endsWith('/task-counts')
        ? { counts: {}, active_tasks: {} }
        : { directions: [] }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ));
  });

  test('viewer loads project reads without global settings or mutation requests', async () => {
    const { result } = renderHook(() => useDirectionsData({
      selectedProjectId: 'project-1',
      addToast: vi.fn(),
      canOperateProject: false,
      canManageGlobalSettings: false,
    }));

    await waitFor(() => {
      expect(apiSpies.apiFetch).toHaveBeenCalledWith(
        '/api/admin/projects/project-1/directions',
      );
    });
    expect(configSpies.fetchCooldownSetting).not.toHaveBeenCalled();
    expect(configSpies.fetchSettings).not.toHaveBeenCalled();

    await act(async () => {
      await result.current.triggerCycle('full');
      await result.current.updateDirection('direction-1');
      await result.current.quickToggleStatus('direction-1', 'active');
      await result.current.linkDirectionToGap('direction-1', 'gap-1');
      await result.current.toggleAutonomous();
      await result.current.triggerResearch('direction-1');
      await result.current.deleteDirection('direction-1');
    });

    const mutationCalls = apiSpies.apiFetch.mock.calls.filter(([, init]) =>
      Boolean((init as RequestInit | undefined)?.method),
    );
    expect(mutationCalls).toEqual([]);
  });

  test('callbacks captured before an operator downgrade become inert immediately', async () => {
    const { result, rerender } = renderHook(
      ({ canOperateProject }) => useDirectionsData({
        selectedProjectId: 'project-1',
        addToast: vi.fn(),
        canOperateProject,
        canManageGlobalSettings: false,
      }),
      { initialProps: { canOperateProject: true } },
    );

    await waitFor(() => {
      expect(apiSpies.apiFetch).toHaveBeenCalledWith(
        '/api/admin/projects/project-1/directions',
      );
    });
    const stale = result.current;
    rerender({ canOperateProject: false });
    apiSpies.apiFetch.mockClear();
    apiSpies.submitChurnRetirementDecision.mockClear();
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);

    await act(async () => {
      await stale.triggerCycle('full');
      await stale.addDirection();
      await stale.updateDirection('direction-1');
      await stale.quickToggleStatus('direction-1', 'active');
      await stale.reactivateDirection('direction-1');
      await stale.activateStagedDirection('direction-1');
      await stale.retargetStagedDirection('direction-1', 12);
      await stale.cancelStagedDirection('direction-1');
      await stale.unholdDirection('direction-1');
      await stale.markDirectionComplete('direction-1');
      await stale.approveProposal('direction-1');
      await stale.rejectProposal('direction-1');
      await stale.reviveProposal('direction-1');
      await stale.applyDirectionUpdate({
        id: 'direction-1',
        pending_update: { suggested_text: 'forbidden stale update' },
      } as never);
      await stale.dismissDirectionUpdate('direction-1');
      await stale.deleteDirection('direction-1');
      await stale.toggleAutonomous();
      await stale.reorderDirections([]);
      await stale.triggerResearch('direction-1');
      await stale.decideChurnRetirement('direction-1', 'RETIRE', 'stale');
      await stale.linkDirectionToGap('direction-1', 'gap-1');
    });

    expect(apiSpies.apiFetch).not.toHaveBeenCalled();
    expect(apiSpies.submitChurnRetirementDecision).not.toHaveBeenCalled();
    expect(confirm).not.toHaveBeenCalled();
    confirm.mockRestore();
  });

  test('late project A reads cannot repopulate project B state', async () => {
    const responseResolvers = new Map<string, (response: Response) => void>();
    const lifecycleResolvers = new Map<string, (value: unknown) => void>();
    apiSpies.apiFetch.mockImplementation((url: string) => new Promise<Response>(resolve => {
      responseResolvers.set(url, resolve);
    }));
    apiSpies.fetchVersionLifecycle.mockImplementation((projectId: string) => new Promise(resolve => {
      lifecycleResolvers.set(projectId, resolve);
    }));
    const addToast = vi.fn();
    const { result, rerender } = renderHook(
      ({ projectId }) => useDirectionsData({
        selectedProjectId: projectId,
        addToast,
        canOperateProject: true,
      }),
      { initialProps: { projectId: 'project-a' as string | null } },
    );
    await waitFor(() => {
      expect(responseResolvers.has('/api/admin/projects/project-a/directions')).toBe(true);
      expect(responseResolvers.has('/api/admin/projects/project-a/directions/task-counts')).toBe(true);
      expect(lifecycleResolvers.has('project-a')).toBe(true);
    });

    rerender({ projectId: 'project-b' });
    await waitFor(() => {
      expect(responseResolvers.has('/api/admin/projects/project-b/directions')).toBe(true);
      expect(responseResolvers.has('/api/admin/projects/project-b/directions/task-counts')).toBe(true);
      expect(lifecycleResolvers.has('project-b')).toBe(true);
    });
    await act(async () => {
      responseResolvers.get('/api/admin/projects/project-b/directions')?.(new Response(JSON.stringify({
        directions: [{ id: 'direction-b', text: 'B', status: 'active', priority: 'high' }],
        autonomous_enabled: true,
        design_doc: { project: 'b' },
        problem_model: { project: 'b' },
        problem_model_size_health: { status: 'healthy' },
        running_update: 'running-b',
        running_update_acknowledged_at: '2026-08-02T00:00:00Z',
        running_update_reconciliation: { status: 'current' },
        self_reflection_log: [{ reflection: 'b' }],
      }), { status: 200 }));
      responseResolvers.get('/api/admin/projects/project-b/directions/task-counts')?.(new Response(JSON.stringify({
        counts: { 'direction-b': 3 },
        active_tasks: { 'direction-b': [{ id: 'task-b', status: 'IN_PROGRESS', title: 'B task' }] },
      }), { status: 200 }));
      lifecycleResolvers.get('project-b')?.({ current_version: { version_number: 9, status: 'OPEN' } });
    });
    await waitFor(() => expect(result.current.directions[0]?.id).toBe('direction-b'));

    await act(async () => {
      responseResolvers.get('/api/admin/projects/project-a/directions')?.(new Response(JSON.stringify({
        directions: [{ id: 'direction-a', text: 'A', status: 'active', priority: 'low' }],
        autonomous_enabled: false,
        design_doc: { project: 'a' },
        problem_model: { project: 'a' },
        problem_model_size_health: { status: 'oversized' },
        running_update: 'running-a',
        running_update_acknowledged_at: '2026-08-01T00:00:00Z',
        running_update_reconciliation: { status: 'stale' },
        self_reflection_log: [{ reflection: 'a' }],
      }), { status: 200 }));
      responseResolvers.get('/api/admin/projects/project-a/directions/task-counts')?.(new Response(JSON.stringify({
        counts: { 'direction-a': 99 },
        active_tasks: { 'direction-a': [{ id: 'task-a', status: 'IN_PROGRESS', title: 'A task' }] },
      }), { status: 200 }));
      lifecycleResolvers.get('project-a')?.({ current_version: { version_number: 4, status: 'OPEN' } });
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(result.current.directions.map(direction => direction.id)).toEqual(['direction-b']);
    expect(result.current.autonomousEnabled).toBe(true);
    expect(result.current.designDoc).toEqual({ project: 'b' });
    expect(result.current.problemModel).toEqual({ project: 'b' });
    expect(result.current.problemModelSizeHealth).toEqual({ status: 'healthy' });
    expect(result.current.runningUpdate).toBe('running-b');
    expect(result.current.runningUpdateAckAt).toBe('2026-08-02T00:00:00Z');
    expect(result.current.runningUpdateReconciliation).toEqual({ status: 'current' });
    expect(result.current.selfReflectionLog).toEqual([{ reflection: 'b' }]);
    expect(result.current.taskCounts).toEqual({ 'direction-b': 3 });
    expect(result.current.activeTasks).toHaveProperty('direction-b');
    expect(result.current.openVersionNumber).toBe(9);
    expect(result.current.currentVersion).toMatchObject({ version_number: 9 });
    expect(addToast).not.toHaveBeenCalled();
  });

  test('scope loss clears state and ignores a deferred project response', async () => {
    const responseResolvers = new Map<string, (response: Response) => void>();
    let resolveLifecycle: ((value: unknown) => void) | undefined;
    apiSpies.apiFetch.mockImplementation((url: string) => new Promise<Response>(resolve => {
      responseResolvers.set(url, resolve);
    }));
    apiSpies.fetchVersionLifecycle.mockImplementation(() => new Promise(resolve => {
      resolveLifecycle = resolve;
    }));
    const addToast = vi.fn();
    const { result, rerender } = renderHook(
      ({ projectId }) => useDirectionsData({ selectedProjectId: projectId, addToast, canOperateProject: true }),
      { initialProps: { projectId: 'project-a' as string | null } },
    );
    await waitFor(() => expect(responseResolvers.size).toBe(2));

    rerender({ projectId: null });
    expect(result.current.directions).toEqual([]);
    expect(result.current.problemModel).toEqual({});
    expect(result.current.runningUpdate).toBeNull();
    expect(result.current.taskCounts).toEqual({});
    expect(result.current.currentVersion).toBeNull();

    await act(async () => {
      responseResolvers.get('/api/admin/projects/project-a/directions')?.(new Response(JSON.stringify({
        directions: [{ id: 'direction-a' }],
        problem_model: { stale: true },
        running_update: 'stale',
      }), { status: 200 }));
      responseResolvers.get('/api/admin/projects/project-a/directions/task-counts')?.(new Response(JSON.stringify({ counts: { 'direction-a': 1 } }), { status: 200 }));
      resolveLifecycle?.({ current_version: { version_number: 1 } });
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(result.current.directions).toEqual([]);
    expect(result.current.problemModel).toEqual({});
    expect(result.current.runningUpdate).toBeNull();
    expect(result.current.taskCounts).toEqual({});
    expect(result.current.currentVersion).toBeNull();
    expect(result.current.loading).toBe(false);
    expect(addToast).not.toHaveBeenCalled();
  });

  test('a project A mutation callback stays inert after switching to operable project B', async () => {
    const { result, rerender } = renderHook(
      ({ projectId }) => useDirectionsData({
        selectedProjectId: projectId,
        addToast: vi.fn(),
        canOperateProject: true,
      }),
      { initialProps: { projectId: 'project-a' } },
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    const staleTrigger = result.current.triggerCycle;
    const staleUpdate = result.current.updateDirection;

    rerender({ projectId: 'project-b' });
    await waitFor(() => expect(result.current.loading).toBe(false));
    apiSpies.apiFetch.mockClear();
    await act(async () => {
      await staleTrigger('full');
      await staleUpdate('direction-a');
    });

    expect(apiSpies.apiFetch).not.toHaveBeenCalled();
  });

  test('in-flight project A mutations cannot update project B UI after switching scope', async () => {
    const mutationResolvers = new Map<string, (response: Response) => void>();
    apiSpies.apiFetch.mockImplementation((url: string, init?: RequestInit) => {
      if (init?.method) {
        return new Promise<Response>(resolve => {
          mutationResolvers.set(`${init.method} ${url}`, resolve);
        });
      }
      const projectId = url.includes('/project-b/') ? 'b' : 'a';
      return Promise.resolve(new Response(JSON.stringify(url.endsWith('/task-counts')
        ? { counts: {}, active_tasks: {} }
        : {
          directions: [{ id: `direction-${projectId}`, text: projectId.toUpperCase(), status: 'active', priority: 'high' }],
        }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    });
    const addToast = vi.fn();
    const { result, rerender } = renderHook(
      ({ projectId }) => useDirectionsData({ selectedProjectId: projectId, addToast, canOperateProject: true }),
      { initialProps: { projectId: 'project-a' } },
    );
    await waitFor(() => expect(result.current.directions[0]?.id).toBe('direction-a'));

    let approve!: Promise<void>;
    let research!: Promise<void>;
    let reorder!: Promise<void>;
    act(() => {
      approve = result.current.approveProposal('direction-a');
      research = result.current.triggerResearch('direction-a');
      reorder = result.current.reorderDirections(result.current.directions);
    });
    expect(mutationResolvers.has('PATCH /api/admin/projects/project-a/directions/direction-a')).toBe(true);
    expect(mutationResolvers.has('POST /api/admin/projects/project-a/directions/direction-a/research')).toBe(true);
    expect(mutationResolvers.has('PATCH /api/admin/projects/project-a/directions/reorder')).toBe(true);

    rerender({ projectId: 'project-b' });
    await waitFor(() => expect(result.current.directions[0]?.id).toBe('direction-b'));
    const readCountBeforeCompletion = apiSpies.apiFetch.mock.calls.filter(([, init]) => !(init as RequestInit | undefined)?.method).length;
    const timeoutSpy = vi.spyOn(globalThis, 'setTimeout');

    await act(async () => {
      mutationResolvers.get('PATCH /api/admin/projects/project-a/directions/direction-a')?.(new Response(null, { status: 200 }));
      mutationResolvers.get('POST /api/admin/projects/project-a/directions/direction-a/research')?.(new Response(null, { status: 200 }));
      mutationResolvers.get('PATCH /api/admin/projects/project-a/directions/reorder')?.(new Response(null, { status: 500 }));
      await Promise.all([approve, research, reorder]);
    });

    expect(result.current.directions.map(direction => direction.id)).toEqual(['direction-b']);
    expect(result.current.error).toBeNull();
    expect(result.current.researchingIds).toEqual({});
    expect(result.current.researchSuccessIds).toEqual({});
    expect(addToast).not.toHaveBeenCalled();
    expect(apiSpies.apiFetch.mock.calls.filter(([, init]) => !(init as RequestInit | undefined)?.method)).toHaveLength(readCountBeforeCompletion);
    expect(timeoutSpy.mock.calls.some(([, delay]) => delay === 3000)).toBe(false);
    timeoutSpy.mockRestore();
  });
});
