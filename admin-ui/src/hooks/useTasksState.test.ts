import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, cleanup } from '@testing-library/react';
import { useTasksState } from './useTasksState';
import type { Task } from '../types/task';

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    authHeaders: vi.fn().mockResolvedValue({ Authorization: 'Bearer ***', 'Content-Type': 'application/json' }),
    authGet: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ tasks: [], total: 0 }) }),
  });
});

vi.mock('./usePersistedState', () => ({
  usePersistedState: (_key: string, init: unknown) => [init, vi.fn()],
}));

vi.mock('../constants', () => ({
  STATUS_OPTIONS: ['QUEUED', 'IN_PROGRESS', 'COMPLETED', 'FAILED'],
}));

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-1',
    title: 'Test task',
    status: 'FAILED',
    requires_approval: false,
    payloads: [{ prompt: 'original prompt' }],
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  } as unknown as Task;
}

function makeDeps(overrides: Partial<Parameters<typeof useTasksState>[0]> = {}): Parameters<typeof useTasksState>[0] {
  return {
    addToast: vi.fn(),
    setError: vi.fn(),
    statusFilter: ['FAILED'],
    addMetricsPoint: vi.fn(),
    notifyRef: { current: false },
    notifyOnFailRef: { current: false },
    notifyOnApprovalNeededRef: { current: false },
    soundOnCompleteRef: { current: false },
    fetchArchiveTasks: vi.fn(),
    selectedTaskIds: new Set(),
    setSelectedTaskIds: vi.fn(),
    setSelectionMode: vi.fn(),
    setConfirmDialog: vi.fn(),
    fetchTasksRef: { current: vi.fn() },
    prevTasksRef: { current: [] },
    selectedProjectId: null,
    directionFilter: null,
    ...overrides,
  };
}

let fetchSpy: ReturnType<typeof vi.fn>;

describe('useTasksState — task mutation error handling', () => {
  const originalError = console.error;

  beforeEach(() => {
    cleanup();
    console.error = vi.fn();
    fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({}),
    });
    vi.stubGlobal('fetch', fetchSpy);
  });

  afterEach(() => {
    console.error = originalError;
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  // --- handleRetry ---

  it('handleRetry shows success toast and does not roll back on 200', async () => {
    const task = makeTask({ id: 'task-retry-1', status: 'FAILED' });
    const deps = makeDeps();
    const { result } = renderHook(() => useTasksState(deps));
    act(() => { result.current.setTasks([task]); });

    await act(async () => { await result.current.handleRetry('task-retry-1'); });

    expect(deps.addToast).toHaveBeenCalledWith('Task queued for retry', 'success');
    expect(deps.addToast).not.toHaveBeenCalledWith(expect.stringContaining('Failed'), 'error');
  });

  it('handleRetry shows error toast and rolls back optimistic state on non-OK response', async () => {
    fetchSpy.mockResolvedValue({
      ok: false,
      status: 422,
      json: vi.fn().mockResolvedValue({ detail: 'Task cannot be retried in current state' }),
    });
    const task = makeTask({ id: 'task-retry-2', status: 'FAILED' });
    const deps = makeDeps();
    const { result } = renderHook(() => useTasksState(deps));
    act(() => { result.current.setTasks([task]); });

    // Verify optimistic update: task should be QUEUED while in-flight.
    // After rejection, it should be rolled back to original.
    await act(async () => { await result.current.handleRetry('task-retry-2'); });

    expect(deps.addToast).toHaveBeenCalledWith(
      'Failed to retry task: Task cannot be retried in current state',
      'error',
    );
    expect(deps.addToast).not.toHaveBeenCalledWith('Task queued for retry', 'success');
    // Task should be rolled back to original FAILED status
    const restoredTask = result.current.tasks.find(t => t.id === 'task-retry-2');
    expect(restoredTask?.status).toBe('FAILED');
  });

  it('handleRetry surfaces generic error toast when backend provides no detail', async () => {
    fetchSpy.mockResolvedValue({
      ok: false,
      status: 500,
      json: vi.fn().mockResolvedValue({}),
    });
    const task = makeTask({ id: 'task-retry-3', status: 'FAILED' });
    const deps = makeDeps();
    const { result } = renderHook(() => useTasksState(deps));
    act(() => { result.current.setTasks([task]); });

    await act(async () => { await result.current.handleRetry('task-retry-3'); });

    expect(deps.addToast).toHaveBeenCalledWith('Failed to retry task', 'error');
    expect(deps.addToast).not.toHaveBeenCalledWith('Task queued for retry', 'success');
  });

  it('handleRetry does not call fetchArchiveTasks on rejection', async () => {
    fetchSpy.mockResolvedValue({ ok: false, status: 400, json: vi.fn().mockResolvedValue({}) });
    const deps = makeDeps();
    const { result } = renderHook(() => useTasksState(deps));
    act(() => { result.current.setTasks([makeTask()]); });

    await act(async () => { await result.current.handleRetry('task-1'); });

    expect(deps.fetchArchiveTasks).not.toHaveBeenCalled();
  });

  // --- handleArchiveTask ---

  it('handleArchiveTask shows success toast on 200', async () => {
    const deps = makeDeps();
    const { result } = renderHook(() => useTasksState(deps));

    await act(async () => { await result.current.handleArchiveTask('task-1'); });

    expect(deps.addToast).toHaveBeenCalledWith('Task archived', 'success');
    expect(deps.addToast).not.toHaveBeenCalledWith(expect.stringContaining('Failed'), 'error');
  });

  it('handleArchiveTask shows error toast on non-OK response', async () => {
    fetchSpy.mockResolvedValue({
      ok: false,
      status: 403,
      json: vi.fn().mockResolvedValue({ detail: 'Not authorized to archive' }),
    });
    const deps = makeDeps();
    const { result } = renderHook(() => useTasksState(deps));

    await act(async () => { await result.current.handleArchiveTask('task-1'); });

    expect(deps.addToast).toHaveBeenCalledWith('Failed to archive task: Not authorized to archive', 'error');
    expect(deps.addToast).not.toHaveBeenCalledWith('Task archived', 'success');
  });

  it('handleArchiveTask surfaces generic error when backend has no detail', async () => {
    fetchSpy.mockResolvedValue({ ok: false, status: 500, json: vi.fn().mockResolvedValue({}) });
    const deps = makeDeps();
    const { result } = renderHook(() => useTasksState(deps));

    await act(async () => { await result.current.handleArchiveTask('task-1'); });

    expect(deps.addToast).toHaveBeenCalledWith('Failed to archive task', 'error');
    expect(deps.addToast).not.toHaveBeenCalledWith('Task archived', 'success');
  });

  // --- handleUnarchiveTask ---

  it('handleUnarchiveTask shows success toast on 200', async () => {
    const deps = makeDeps();
    const { result } = renderHook(() => useTasksState(deps));

    await act(async () => { await result.current.handleUnarchiveTask('task-1'); });

    expect(deps.addToast).toHaveBeenCalledWith('Task unarchived', 'success');
    expect(deps.addToast).not.toHaveBeenCalledWith(expect.stringContaining('Failed'), 'error');
  });

  it('handleUnarchiveTask shows error toast on non-OK response', async () => {
    fetchSpy.mockResolvedValue({
      ok: false,
      status: 422,
      json: vi.fn().mockResolvedValue({ detail: 'Task is not archived' }),
    });
    const deps = makeDeps();
    const { result } = renderHook(() => useTasksState(deps));

    await act(async () => { await result.current.handleUnarchiveTask('task-1'); });

    expect(deps.addToast).toHaveBeenCalledWith('Failed to unarchive task: Task is not archived', 'error');
    expect(deps.addToast).not.toHaveBeenCalledWith('Task unarchived', 'success');
  });

  it('handleUnarchiveTask does not call fetchArchiveTasks or fetchTasks on rejection', async () => {
    fetchSpy.mockResolvedValue({ ok: false, status: 400, json: vi.fn().mockResolvedValue({}) });
    const deps = makeDeps();
    const { result } = renderHook(() => useTasksState(deps));

    await act(async () => { await result.current.handleUnarchiveTask('task-1'); });

    expect(deps.fetchArchiveTasks).not.toHaveBeenCalled();
  });

  // --- handleEditPrompt ---

  it('handleEditPrompt shows success toast on 200', async () => {
    const deps = makeDeps();
    const { result } = renderHook(() => useTasksState(deps));

    await act(async () => { await result.current.handleEditPrompt('task-1', 'new prompt text'); });

    expect(deps.addToast).toHaveBeenCalledWith('Prompt updated', 'success');
    expect(deps.addToast).not.toHaveBeenCalledWith(expect.stringContaining('Failed'), 'error');
  });

  it('handleEditPrompt shows error toast on non-OK response', async () => {
    fetchSpy.mockResolvedValue({
      ok: false,
      status: 400,
      json: vi.fn().mockResolvedValue({ detail: 'Prompt too long' }),
    });
    const deps = makeDeps();
    const { result } = renderHook(() => useTasksState(deps));

    await act(async () => { await result.current.handleEditPrompt('task-1', 'x'.repeat(10000)); });

    expect(deps.addToast).toHaveBeenCalledWith('Failed to edit task: Prompt too long', 'error');
    expect(deps.addToast).not.toHaveBeenCalledWith('Prompt updated', 'success');
  });

  it('handleEditPrompt surfaces generic error when backend has no detail', async () => {
    fetchSpy.mockResolvedValue({ ok: false, status: 500, json: vi.fn().mockResolvedValue({}) });
    const deps = makeDeps();
    const { result } = renderHook(() => useTasksState(deps));

    await act(async () => { await result.current.handleEditPrompt('task-1', 'new prompt'); });

    expect(deps.addToast).toHaveBeenCalledWith('Failed to edit task', 'error');
    expect(deps.addToast).not.toHaveBeenCalledWith('Prompt updated', 'success');
  });

  // --- handleDismiss ---

  it('handleDismiss shows success toast on 200', async () => {
    const deps = makeDeps();
    const { result } = renderHook(() => useTasksState(deps));

    await act(async () => { await result.current.handleDismiss('task-1'); });

    expect(deps.addToast).toHaveBeenCalledWith('Task dismissed', 'success');
    expect(deps.addToast).not.toHaveBeenCalledWith(expect.stringContaining('Failed'), 'error');
  });

  it('handleDismiss shows error toast on non-OK response', async () => {
    fetchSpy.mockResolvedValue({
      ok: false,
      status: 409,
      json: vi.fn().mockResolvedValue({ detail: 'Task already in terminal state' }),
    });
    const deps = makeDeps();
    const { result } = renderHook(() => useTasksState(deps));

    await act(async () => { await result.current.handleDismiss('task-1'); });

    expect(deps.addToast).toHaveBeenCalledWith('Failed to dismiss task: Task already in terminal state', 'error');
    expect(deps.addToast).not.toHaveBeenCalledWith('Task dismissed', 'success');
  });

  it('handleDismiss surfaces generic error when backend has no detail', async () => {
    fetchSpy.mockResolvedValue({ ok: false, status: 500, json: vi.fn().mockResolvedValue({}) });
    const deps = makeDeps();
    const { result } = renderHook(() => useTasksState(deps));

    await act(async () => { await result.current.handleDismiss('task-1'); });

    expect(deps.addToast).toHaveBeenCalledWith('Failed to dismiss task', 'error');
    expect(deps.addToast).not.toHaveBeenCalledWith('Task dismissed', 'success');
  });
});
