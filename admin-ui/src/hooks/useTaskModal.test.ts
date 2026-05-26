import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { useTaskModal } from './useTaskModal';
import { authGet } from '../api';

vi.mock('../api', () => ({
  authHeaders: vi.fn().mockResolvedValue({ Authorization: 'Bearer ***', 'Content-Type': 'application/json' }),
  authGet: vi.fn(),
  API_BASE_URL: '',
}));

const mockAuthGet = vi.mocked(authGet);

function okGet(data: unknown) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve(data) } as Response);
}

let fetchSpy: ReturnType<typeof vi.fn>;

describe('useTaskModal', () => {
  const originalError = console.error;

  beforeEach(() => {
    console.error = vi.fn();
    fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({}),
    });
    vi.stubGlobal('fetch', fetchSpy);
    localStorage.clear();
    // Default: repos endpoint returns empty list
    mockAuthGet.mockImplementation((url: string) => {
      if (url.includes('/repos')) return okGet({ repositories: [] });
      if (url.includes('/branches')) return okGet({ branches: [] });
      return okGet({});
    });
  });

  afterEach(() => {
    console.error = originalError;
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  // --- Initial state ---

  it('showNewTaskModal starts as false', () => {
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    expect(result.current.showNewTaskModal).toBe(false);
  });

  it('returns correct initial form field values', () => {
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    expect(result.current.newTaskRepo).toBe('');
    expect(result.current.newTaskBranch).toBe('main');
    expect(result.current.newTaskTitle).toBe('');
    expect(result.current.newTaskPrompt).toBe('');
    expect(result.current.newTaskType).toBe('create');
    expect(result.current.newTaskPrNumber).toBe('');
    expect(result.current.newTaskPrBranch).toBe('');
    expect(result.current.newTaskModel).toBe('');
    expect(result.current.newTaskEffort).toBe('');
  });

  it('returns false for useManualRepo and useManualBranch initially', () => {
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    expect(result.current.useManualRepo).toBe(false);
    expect(result.current.useManualBranch).toBe(false);
    expect(result.current.repoOptions).toEqual([]);
    expect(result.current.branchOptions).toEqual([]);
  });

  // --- Modal open/close state ---

  it('openNewTaskModal sets showNewTaskModal to true', async () => {
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    await act(async () => {
      result.current.openNewTaskModal();
    });
    expect(result.current.showNewTaskModal).toBe(true);
  });

  it('openNewTaskModal sets task type from defaultTaskType', async () => {
    const { result } = renderHook(() =>
      useTaskModal('research', vi.fn(), { current: vi.fn() }),
    );
    await act(async () => {
      result.current.openNewTaskModal();
    });
    expect(result.current.newTaskType).toBe('research');
  });

  it('openNewTaskModal uses the selected project branch default', async () => {
    const defaultTaskBranchRef = { current: 'automain' };
    const selectedProjectRepoRef = { current: 'owner/repo' };
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }, defaultTaskBranchRef, selectedProjectRepoRef),
    );
    act(() => {
      result.current.setNewTaskBranch('feature/custom');
    });
    await act(async () => {
      result.current.openNewTaskModal();
    });
    expect(result.current.newTaskBranch).toBe('automain');
  });

  it('setShowNewTaskModal(false) closes the modal', async () => {
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    await act(async () => {
      result.current.openNewTaskModal();
    });
    expect(result.current.showNewTaskModal).toBe(true);
    act(() => {
      result.current.setShowNewTaskModal(false);
    });
    expect(result.current.showNewTaskModal).toBe(false);
  });

  // --- Form field setters ---

  it('setNewTaskTitle updates the title field', () => {
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    act(() => {
      result.current.setNewTaskTitle('My new task title');
    });
    expect(result.current.newTaskTitle).toBe('My new task title');
  });

  it('setNewTaskBranch updates the branch field', () => {
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    act(() => {
      result.current.setNewTaskBranch('feature/my-feature');
    });
    expect(result.current.newTaskBranch).toBe('feature/my-feature');
  });

  it('setNewTaskType updates the task type', () => {
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    act(() => {
      result.current.setNewTaskType('refactor');
    });
    expect(result.current.newTaskType).toBe('refactor');
  });

  it('setNewTaskPrNumber updates the PR number', () => {
    const { result } = renderHook(() =>
      useTaskModal('review', vi.fn(), { current: vi.fn() }),
    );
    act(() => {
      result.current.setNewTaskPrNumber('42');
    });
    expect(result.current.newTaskPrNumber).toBe('42');
  });

  // --- Repo and branch option loading ---

  it('openNewTaskModal fetches repo options', async () => {
    const repos = [
      { full_name: 'owner/repo-a', default_branch: 'main' },
      { full_name: 'owner/repo-b', default_branch: 'master' },
    ];
    mockAuthGet.mockImplementation((url: string) => {
      if (url.includes('/repos')) return okGet({ repositories: repos });
      return okGet({});
    });
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    await act(async () => {
      result.current.openNewTaskModal();
    });
    await waitFor(() => {
      expect(result.current.repoOptions).toEqual(repos);
      expect(result.current.repoLoading).toBe(false);
    });
  });

  it('fetchRepoOptions sets useManualRepo and repoError on API error', async () => {
    mockAuthGet.mockImplementation((url: string) => {
      if (url.includes('/repos')) {
        return Promise.resolve({
          ok: false,
          status: 403,
          json: () => Promise.resolve({ detail: 'Not authorized' }),
        } as Response);
      }
      return okGet({});
    });
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    await act(async () => {
      result.current.openNewTaskModal();
    });
    await waitFor(() => {
      expect(result.current.useManualRepo).toBe(true);
      expect(result.current.repoError).toBe('Not authorized');
    });
  });

  it('fetchRepoOptions sets useManualRepo on network failure', async () => {
    mockAuthGet.mockImplementation((url: string) => {
      if (url.includes('/repos')) throw new Error('Network failure');
      return okGet({});
    });
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    await act(async () => {
      result.current.openNewTaskModal();
    });
    await waitFor(() => {
      expect(result.current.useManualRepo).toBe(true);
      expect(result.current.repoError).toBe('Failed to connect to server');
    });
  });

  it('handleRepoChange fetches branch options for a valid repo', async () => {
    const branches = ['main', 'dev', 'feature/x'];
    mockAuthGet.mockImplementation((url: string) => {
      if (url.includes('/branches')) return okGet({ branches });
      return okGet({ repositories: [] });
    });
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    await act(async () => {
      result.current.handleRepoChange('owner/repo');
    });
    expect(result.current.newTaskRepo).toBe('owner/repo');
    await waitFor(() => {
      expect(result.current.branchOptions).toEqual(branches);
    });
  });

  it('handleRepoChange sets default branch from repoOptions metadata', async () => {
    const repos = [{ full_name: 'owner/repo', default_branch: 'develop' }];
    mockAuthGet.mockImplementation((url: string) => {
      if (url.includes('/repos')) return okGet({ repositories: repos });
      if (url.includes('/branches')) return okGet({ branches: ['develop'] });
      return okGet({});
    });
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    // Populate repoOptions first
    await act(async () => {
      result.current.openNewTaskModal();
    });
    await waitFor(() => expect(result.current.repoOptions).toHaveLength(1));
    // Now change repo — it should pick up the default_branch
    await act(async () => {
      result.current.handleRepoChange('owner/repo');
    });
    expect(result.current.newTaskBranch).toBe('develop');
  });

  it('handleRepoChange uses the selected project branch for the selected project repo', async () => {
    const defaultTaskBranchRef = { current: 'automain' };
    const selectedProjectRepoRef = { current: 'owner/repo' };
    mockAuthGet.mockImplementation((url: string) => {
      if (url.includes('/branches')) return okGet({ branches: ['automain', 'main'] });
      return okGet({ repositories: [] });
    });
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }, defaultTaskBranchRef, selectedProjectRepoRef),
    );
    await act(async () => {
      result.current.handleRepoChange('owner/repo');
    });
    expect(result.current.newTaskBranch).toBe('automain');
  });

  it('handleRepoChange clears branch options when repo has no slash', async () => {
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    act(() => {
      result.current.setBranchOptions(['main', 'dev']);
    });
    act(() => {
      result.current.handleRepoChange('no-slash-repo');
    });
    expect(result.current.branchOptions).toEqual([]);
  });

  // --- Task submission ---

  it('handleCreateTask calls POST /api/admin/tasks with form data', async () => {
    fetchSpy.mockResolvedValue({ ok: true, json: vi.fn().mockResolvedValue({ id: 'task-1' }) });
    const addToast = vi.fn();
    const fetchTasksRef = { current: vi.fn() };
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, fetchTasksRef),
    );
    act(() => {
      result.current.setNewTaskRepo('owner/repo');
      result.current.setNewTaskTitle('Build feature');
      result.current.setNewTaskPrompt('Make it work');
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.stringContaining('/api/admin/tasks'),
      expect.objectContaining({ method: 'POST' }),
    );
    expect(addToast).toHaveBeenCalledWith('Task created successfully', 'success');
    expect(fetchTasksRef.current).toHaveBeenCalled();
  });

  it('handleCreateTask closes modal and resets title/prompt on success', async () => {
    fetchSpy.mockResolvedValue({ ok: true, json: vi.fn().mockResolvedValue({}) });
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    act(() => {
      result.current.setShowNewTaskModal(true);
      result.current.setNewTaskTitle('Some title');
      result.current.setNewTaskPrompt('Some prompt');
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(result.current.showNewTaskModal).toBe(false);
    expect(result.current.newTaskTitle).toBe('');
    expect(result.current.newTaskPrompt).toBe('');
  });

  it('handleCreateTask shows error toast on API failure', async () => {
    fetchSpy.mockResolvedValue({
      ok: false,
      statusText: 'Bad Request',
      json: vi.fn().mockResolvedValue({ detail: 'Invalid payload' }),
    });
    const addToast = vi.fn();
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, { current: vi.fn() }),
    );
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(addToast).toHaveBeenCalledWith(
      expect.stringContaining('Failed to create task'),
      'error',
    );
  });

  it('handleCreateTask rejects review task with no PR number', async () => {
    const addToast = vi.fn();
    const { result } = renderHook(() =>
      useTaskModal('review', addToast, { current: vi.fn() }),
    );
    act(() => {
      result.current.setNewTaskType('review');
      result.current.setNewTaskPrNumber('');
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(addToast).toHaveBeenCalledWith('PR number must be a positive integer', 'error');
    expect(fetchSpy).not.toHaveBeenCalledWith(
      expect.stringContaining('/api/admin/tasks'),
      expect.anything(),
    );
  });

  it('handleCreateTask rejects review task with non-numeric PR number', async () => {
    const addToast = vi.fn();
    const { result } = renderHook(() =>
      useTaskModal('review', addToast, { current: vi.fn() }),
    );
    act(() => {
      result.current.setNewTaskType('review');
      result.current.setNewTaskPrNumber('abc');
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(addToast).toHaveBeenCalledWith('PR number must be a positive integer', 'error');
  });

  it('handleCreateTask rejects fix task with missing PR branch', async () => {
    const addToast = vi.fn();
    const { result } = renderHook(() =>
      useTaskModal('fix', addToast, { current: vi.fn() }),
    );
    act(() => {
      result.current.setNewTaskType('fix');
      result.current.setNewTaskPrNumber('42');
      result.current.setNewTaskPrBranch('');
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(addToast).toHaveBeenCalledWith('PR branch is required for fix tasks', 'error');
  });

  // --- localStorage integration ---

  it('getDefaultModelForType reads task-type-specific model from localStorage', () => {
    localStorage.setItem(
      'blueprint-admin:taskTypeModel:create',
      JSON.stringify('claude-opus-4-6'),
    );
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    expect(result.current.getDefaultModelForType('create')).toBe('claude-opus-4-6');
  });

  it('getDefaultModelForType falls back to defaultModel when type-specific is absent', () => {
    localStorage.setItem('blueprint-admin:defaultModel', JSON.stringify('claude-haiku-4-5'));
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    expect(result.current.getDefaultModelForType('create')).toBe('claude-haiku-4-5');
  });

  it('getDefaultEffortForType reads effort from localStorage', () => {
    localStorage.setItem(
      'blueprint-admin:taskTypeEffort:research',
      JSON.stringify('high'),
    );
    const { result } = renderHook(() =>
      useTaskModal('research', vi.fn(), { current: vi.fn() }),
    );
    expect(result.current.getDefaultEffortForType('research')).toBe('high');
  });
});
