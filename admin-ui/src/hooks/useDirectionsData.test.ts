import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, waitFor, cleanup } from '@testing-library/react';
import { useDirectionsData } from './useDirectionsData';
import { apiFetch, fetchVersionLifecycle } from '../api';

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock();
});

vi.mock('./useAutonomousConfig', () => ({
  useAutonomousConfig: () => ({
    fetchCooldownSetting: vi.fn(),
    fetchSettings: vi.fn(),
    configCooldownHours: 24,
    setConfigCooldownHours: vi.fn(),
    configIntervalMinutes: 60,
    setConfigIntervalMinutes: vi.fn(),
    configMaxParallel: 3,
    setConfigMaxParallel: vi.fn(),
    configMaxTotalResearch: 5,
    setConfigMaxTotalResearch: vi.fn(),
    configMaxTasksPerHour: 10,
    setConfigMaxTasksPerHour: vi.fn(),
    autonomousConfigOpen: false,
    setAutonomousConfigOpen: vi.fn(),
    savingConfigField: null,
    saveConfigField: vi.fn(),
    cooldownMinutes: 240,
  }),
}));

vi.mock('./usePoller', () => ({
  DEFAULT_POLL_MS: 30_000,
  usePoller: vi.fn(),
  __pollerInternals: { reset: vi.fn(), hasActiveTimers: vi.fn(), subscriberCount: vi.fn() },
}));

const mockApiFetch = vi.mocked(apiFetch);
const mockFetchVersionLifecycle = vi.mocked(fetchVersionLifecycle);

function okResponse(data: unknown) {
  return Promise.resolve({
    ok: true,
    json: () => Promise.resolve(data),
  } as Response);
}

describe('useDirectionsData addDirection idempotency', () => {
  beforeEach(() => {
    cleanup();
    mockFetchVersionLifecycle.mockResolvedValue({ current_version: null } as Awaited<ReturnType<typeof fetchVersionLifecycle>>);
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('rotates the add-form idempotency key when the draft changes after a failed submit', async () => {
    const postBodies: Array<Record<string, unknown>> = [];
    let addAttempts = 0;

    mockApiFetch.mockImplementation((url: string, init?: RequestInit) => {
      const isAddPost =
        init?.method === 'POST' && url.includes('/directions') && !url.includes('task-counts');
      if (isAddPost) {
        postBodies.push(JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>);
        addAttempts += 1;
        if (addAttempts === 1) {
          return Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve({}) } as Response);
        }
        return okResponse({ direction: { id: 'dir-2' } });
      }
      if (url.includes('/directions/task-counts')) {
        return okResponse({ counts: {}, active_tasks: {} });
      }
      if (url.includes('/directions')) {
        return okResponse({
          directions: [],
          autonomous_enabled: false,
          design_doc: {},
          problem_model: {},
          problem_model_size_health: null,
          running_update: null,
          running_update_acknowledged_at: null,
          running_update_reconciliation: null,
        });
      }
      return okResponse({});
    });

    const { result } = renderHook(() =>
      useDirectionsData({
        selectedProjectId: 'proj-1',
        addToast: vi.fn(),
        canOperateProject: true,
      }),
    );

    await waitFor(() => {
      expect(mockApiFetch).toHaveBeenCalledWith(
        expect.stringContaining('/api/admin/projects/proj-1/directions'),
      );
    });
    mockApiFetch.mockClear();

    act(() => {
      result.current.form.setNewText('Original direction draft');
    });

    await act(async () => {
      await result.current.addDirection();
    });

    await waitFor(() => {
      expect(result.current.error).toBe('Failed to add direction. Please try again.');
    });

    act(() => {
      result.current.form.setNewText('Edited direction draft');
    });

    await act(async () => {
      await result.current.addDirection();
    });

    await waitFor(() => {
      expect(postBodies).toHaveLength(2);
    });

    expect(postBodies[0].text).toBe('Original direction draft');
    expect(postBodies[1].text).toBe('Edited direction draft');
    expect(typeof postBodies[0].idempotency_key).toBe('string');
    expect(typeof postBodies[1].idempotency_key).toBe('string');
    expect(postBodies[0].idempotency_key).not.toBe(postBodies[1].idempotency_key);
  });
});

describe('useDirectionsData lite refresh', () => {
  beforeEach(() => {
    cleanup();
    mockFetchVersionLifecycle.mockResolvedValue({ current_version: null } as Awaited<ReturnType<typeof fetchVersionLifecycle>>);
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('does not blank directions during lite refresh', async () => {
    let liteFetchStarted = false;
    mockApiFetch.mockImplementation((url: string) => {
      if (url.includes('/directions/task-counts')) {
        return okResponse({ counts: {}, active_tasks: {} });
      }
      if (url.includes('/directions?lite=1')) {
        liteFetchStarted = true;
        return new Promise(() => {});
      }
      if (url.includes('/directions')) {
        return okResponse({
          directions: [{ id: 'd1', text: 'Keep visible', priority: 'medium', status: 'active', added_at: '2024-01-01T00:00:00Z' }],
          autonomous_enabled: false,
          design_doc: {},
          problem_model: {},
          problem_model_size_health: null,
          running_update: null,
          running_update_acknowledged_at: null,
          running_update_reconciliation: null,
        });
      }
      return okResponse({});
    });

    const { result } = renderHook(() =>
      useDirectionsData({ selectedProjectId: 'proj-1', addToast: vi.fn() }),
    );

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
      expect(result.current.directions).toHaveLength(1);
    });

    await act(async () => {
      void result.current.fetchDirections({ lite: true });
    });

    await waitFor(() => {
      expect(liteFetchStarted).toBe(true);
    });
    expect(result.current.loading).toBe(false);
    expect(result.current.directions).toHaveLength(1);
  });

  it('preserves unchanged object identities during lite refresh', async () => {
    mockApiFetch.mockImplementation((url: string) => {
      if (url.includes('/directions/task-counts')) {
        return okResponse({ counts: { d1: 2 }, active_tasks: { d1: [{ id: 't1', status: 'running', title: 'Work item' }] } });
      }
      if (url.includes('/directions')) {
        return okResponse({
          directions: [{
            id: 'd1',
            text: 'Keep stable',
            priority: 'medium',
            status: 'active',
            added_at: '2024-01-01T00:00:00Z',
            category: 'product',
          }],
          autonomous_enabled: false,
          design_doc: {},
          problem_model: {},
          problem_model_size_health: null,
          running_update: null,
          running_update_acknowledged_at: null,
          running_update_reconciliation: null,
        });
      }
      return okResponse({});
    });

    const { result } = renderHook(() =>
      useDirectionsData({ selectedProjectId: 'proj-1', addToast: vi.fn() }),
    );

    await waitFor(() => {
      expect(result.current.directions).toHaveLength(1);
    });

    const initialDirections = result.current.directions;
    const initialDirection = result.current.directions[0];
    const initialTaskCounts = result.current.taskCounts;
    const initialActiveTasks = result.current.activeTasks;

    await act(async () => {
      await result.current.fetchDirections({ lite: true });
    });

    expect(result.current.directions).toBe(initialDirections);
    expect(result.current.directions[0]).toBe(initialDirection);
    expect(result.current.taskCounts).toBe(initialTaskCounts);
    expect(result.current.activeTasks).toBe(initialActiveTasks);
  });
});
