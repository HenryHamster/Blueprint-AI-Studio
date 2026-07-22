import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { useTaskModal } from './useTaskModal';
import { authGet, fetchTaskTypeCatalog, createComputeExperiment, listFleetRunners, type TaskTypeCatalogEntry } from '../api';

vi.mock('../api', () => ({
  authHeaders: vi.fn().mockResolvedValue({ Authorization: 'Bearer ***', 'Content-Type': 'application/json' }),
  authGet: vi.fn(),
  fetchTaskTypeCatalog: vi.fn(),
  createComputeExperiment: vi.fn(),
  listFleetRunners: vi.fn(),
  computeExperimentPreflight: vi.fn(),
  API_BASE_URL: '',
}));

const mockAuthGet = vi.mocked(authGet);
const mockFetchTaskTypeCatalog = vi.mocked(fetchTaskTypeCatalog);
const mockCreateComputeExperiment = vi.mocked(createComputeExperiment);
const mockListFleetRunners = vi.mocked(listFleetRunners);

function catalogEntry(overrides: Partial<TaskTypeCatalogEntry> & { id: string }): TaskTypeCatalogEntry {
  return {
    id: overrides.id,
    label: overrides.label ?? overrides.id,
    description: overrides.description ?? '',
    agent_mode: 'write',
    default_category: null,
    user_launchable: true,
    operator_contract_launchable: false,
    provider_configurable: true,
    filter_visible: true,
    output_kind: 'pr',
    requires_code: true,
    requires_web: false,
    requires_compute: false,
    evaluation_strategy: 'ci_and_review',
    cost_ceiling_usd: null,
    run_shape: 'single_agent',
    ...overrides,
  };
}

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
    // Default: catalog fetch returns nothing — keeps the static fallback options.
    mockFetchTaskTypeCatalog.mockResolvedValue([]);
    // Default: the dedicated compute_experiment launch resolves to a created task.
    mockCreateComputeExperiment.mockResolvedValue({ id: 'task-ce-1' });
    // Default: listFleetRunners resolves to an empty array.
    mockListFleetRunners.mockResolvedValue([]);
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
    expect(result.current.newTaskBranch).toBe('');
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

  it('openNewTaskModal leaves base branch empty while project branch config is unresolved', async () => {
    const defaultTaskBranchRef = { current: null };
    const selectedProjectRepoRef = { current: 'owner/repo' };
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }, defaultTaskBranchRef, selectedProjectRepoRef),
    );
    await act(async () => {
      result.current.openNewTaskModal();
    });
    expect(result.current.newTaskBranch).toBe('');
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
      await result.current.handleCreateTask({
        preventDefault: vi.fn(),
      } as unknown as React.FormEvent);
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
      await result.current.handleCreateTask({
        preventDefault: vi.fn(),
      } as unknown as React.FormEvent);
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
      await result.current.handleCreateTask({
        preventDefault: vi.fn(),
      } as unknown as React.FormEvent);
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
      await result.current.handleCreateTask({
        preventDefault: vi.fn(),
      } as unknown as React.FormEvent);
    });
    expect(addToast).toHaveBeenCalledWith('PR number must be a positive integer', 'error');
  });

  it('handleCreateTask rejects review task with missing PR branch', async () => {
    const addToast = vi.fn();
    const { result } = renderHook(() =>
      useTaskModal('review', addToast, { current: vi.fn() }),
    );
    act(() => {
      result.current.setNewTaskType('review');
      result.current.setNewTaskPrNumber('42');
      result.current.setNewTaskPrBranch('');
    });
    await act(async () => {
      await result.current.handleCreateTask({
        preventDefault: vi.fn(),
      } as unknown as React.FormEvent);
    });
    expect(addToast).toHaveBeenCalledWith('PR branch is required for review tasks', 'error');
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

  it('handleCreateTask surfaces a wrapped backend rejection message', async () => {
    // The backend rejects a doomed task with an HTTPException, which the app
    // serializes as { error: { message } }. The operator must see that message,
    // not a silent failure.
    fetchSpy.mockResolvedValue({
      ok: false,
      statusText: 'Bad Request',
      json: vi.fn().mockResolvedValue({
        error: { message: "task_type 'compute_experiment' is not operator-launchable" },
      }),
    });
    const addToast = vi.fn();
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, { current: vi.fn() }),
    );
    act(() => {
      result.current.setNewTaskRepo('owner/repo');
      result.current.setNewTaskTitle('t');
      result.current.setNewTaskPrompt('p');
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(addToast).toHaveBeenCalledWith(
      expect.stringContaining('not operator-launchable'),
      'error',
    );
  });

  // --- Duplicate-task flow: non-launchable types must not be re-emitted ---

  it('openDuplicateTaskModal preserves compute_experiment task_type so it can be re-run', () => {
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    act(() => {
      result.current.openDuplicateTaskModal({
        id: 'task-99',
        title: 'Old compute experiment',
        description: 'rerun it',
        task_type: 'compute_experiment',
        payloads: [{ base_branch: 'main' }],
      } as unknown as Parameters<typeof result.current.openDuplicateTaskModal>[0]);
    });
    // compute_experiment is operator-contract-launchable, so it should be
    // preserved so the operator can re-run the same experiment.
    expect(result.current.newTaskType).toBe('compute_experiment');
  });

  it('openDuplicateTaskModal preserves a launchable task_type', () => {
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    act(() => {
      result.current.openDuplicateTaskModal({
        id: 'task-100',
        title: 'Old research',
        description: 'rerun it',
        task_type: 'research',
        payloads: [{ base_branch: 'main' }],
      } as unknown as Parameters<typeof result.current.openDuplicateTaskModal>[0]);
    });
    expect(result.current.newTaskType).toBe('research');
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

  // --- openPrefilledTaskModal ---

  it('openPrefilledTaskModal sets task type, title, prompt, prNumber, and prBranch', async () => {
    const selectedProjectRepoRef = { current: 'owner/repo' };
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }, undefined, selectedProjectRepoRef),
    );
    await act(async () => {
      result.current.openPrefilledTaskModal({
        taskType: 'web_research',
        title: 'Stuck orphan PR',
        prompt: 'PR #42 is open but has no active task — investigate. PR: https://github.com/acme/repo/pull/42',
        prNumber: 42,
        prBranch: 'fix/stuck',
      });
    });
    expect(result.current.showNewTaskModal).toBe(true);
    expect(result.current.newTaskType).toBe('web_research');
    expect(result.current.newTaskTitle).toBe('Stuck orphan PR');
    expect(result.current.newTaskPrompt).toContain('#42');
    expect(result.current.newTaskPrompt).toContain('https://github.com/acme/repo/pull/42');
    expect(result.current.newTaskPrNumber).toBe('42');
    expect(result.current.newTaskPrBranch).toBe('fix/stuck');
    expect(result.current.newTaskRepo).toBe('owner/repo');
  });

  it('openPrefilledTaskModal sets fix type and base branch from baseBranch', async () => {
    const defaultTaskBranchRef = { current: 'automain' };
    const selectedProjectRepoRef = { current: 'owner/repo' };
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }, defaultTaskBranchRef, selectedProjectRepoRef),
    );
    await act(async () => {
      result.current.openPrefilledTaskModal({
        taskType: 'fix',
        title: 'Fix PR #99',
        prompt: 'Fix orphaned PR',
        prNumber: 99,
        prBranch: 'feature/fix',
        baseBranch: 'release/1.2',
      });
    });
    expect(result.current.newTaskType).toBe('fix');
    expect(result.current.newTaskPrNumber).toBe('99');
    expect(result.current.newTaskPrBranch).toBe('feature/fix');
    // The PR's own base branch wins over the project default.
    expect(result.current.newTaskBranch).toBe('release/1.2');
  });

  it('openPrefilledTaskModal falls back to the project default branch when baseBranch is absent', async () => {
    const defaultTaskBranchRef = { current: 'automain' };
    const selectedProjectRepoRef = { current: 'owner/repo' };
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }, defaultTaskBranchRef, selectedProjectRepoRef),
    );
    await act(async () => {
      result.current.openPrefilledTaskModal({
        taskType: 'fix',
        title: 'Fix PR #1',
        prompt: 'Fix orphaned PR',
        prNumber: 1,
        prBranch: 'feature/x',
      });
    });
    expect(result.current.newTaskBranch).toBe('automain');
  });

  it('openPrefilledTaskModal does NOT autosave the prefilled title/prompt as a draft', async () => {
    const selectedProjectRepoRef = { current: 'owner/repo' };
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }, undefined, selectedProjectRepoRef),
    );
    await act(async () => {
      result.current.openPrefilledTaskModal({
        taskType: 'fix',
        title: 'Prefilled title — should not clobber draft',
        prompt: 'Prefilled prompt — should not clobber draft',
      });
    });
    // Wait past the 500ms debounce window for draft autosave.
    await new Promise(r => setTimeout(r, 700));
    expect(localStorage.getItem('blueprint-admin:draft:owner/repo:title')).toBeNull();
    expect(localStorage.getItem('blueprint-admin:draft:owner/repo:prompt')).toBeNull();
  });

  it('openWebResearchTaskModal pre-fills the modal for a web research launch without autosaving a draft', async () => {
    const selectedProjectRepoRef = { current: 'owner/repo' };
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }, undefined, selectedProjectRepoRef),
    );
    await act(async () => {
      result.current.openWebResearchTaskModal('Postgres pooling behavior');
    });
    expect(result.current.showNewTaskModal).toBe(true);
    expect(result.current.newTaskType).toBe('web_research');
    expect(result.current.newTaskTitle).toContain('Postgres pooling behavior');
    expect(result.current.newTaskPrompt).toContain('Postgres pooling behavior');
    await new Promise(r => setTimeout(r, 700));
    expect(localStorage.getItem('blueprint-admin:draft:owner/repo:title')).toBeNull();
    expect(localStorage.getItem('blueprint-admin:draft:owner/repo:prompt')).toBeNull();
  });

  it('openPrefilledTaskModal triggers fetchRepoOptions (and branches when repo is available)', async () => {
    const selectedProjectRepoRef = { current: 'owner/repo' };
    mockAuthGet.mockClear();
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }, undefined, selectedProjectRepoRef),
    );
    await act(async () => {
      result.current.openPrefilledTaskModal({
        taskType: 'fix',
        title: 't',
        prompt: 'p',
        prNumber: 1,
        prBranch: 'fix/x',
      });
    });
    await waitFor(() => {
      const urls = mockAuthGet.mock.calls.map(c => String(c[0]));
      expect(urls.some(u => u.includes('/api/admin/github/repos'))).toBe(true);
      expect(urls.some(u => u.includes('/api/admin/github/branches'))).toBe(true);
    });
  });

  // --- launchableTaskTypeOptions (backend catalog derivation) ---

  it('starts with the static launchable options before the modal opens', () => {
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    const values = result.current.launchableTaskTypeOptions.map(o => o.value);
    expect(values).toContain('create');
    expect(values).toContain('review');
  });

  it('openNewTaskModal derives launchable options (incl. newly registered types) from the catalog', async () => {
    mockFetchTaskTypeCatalog.mockResolvedValue([
      catalogEntry({ id: 'create', label: 'Create', description: 'Write code, open PR' }),
      catalogEntry({ id: 'fix', label: 'Fix', description: 'Fix a bug on a PR' }),
      catalogEntry({ id: 'strategic_review', label: 'Strategic Review', user_launchable: false }),
    ]);
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    await act(async () => {
      result.current.openNewTaskModal();
    });
    await waitFor(() => {
      const values = result.current.launchableTaskTypeOptions.map(o => o.value);
      expect(values).toContain('fix');
      expect(values).not.toContain('strategic_review');
    });
    const fix = result.current.launchableTaskTypeOptions.find(o => o.value === 'fix');
    expect(fix).toEqual({ value: 'fix', label: 'Fix', description: 'Fix a bug on a PR' });
  });

  it('falls back to the static launchable options when the catalog fetch rejects', async () => {
    mockFetchTaskTypeCatalog.mockRejectedValue(new Error('catalog unreachable'));
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    await act(async () => {
      result.current.openNewTaskModal();
    });
    await waitFor(() => expect(mockFetchTaskTypeCatalog).toHaveBeenCalled());
    const values = result.current.launchableTaskTypeOptions.map(o => o.value);
    expect(values).toContain('create');
    expect(values).toContain('review');
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

  // --- duplicating a compute experiment prefills the launch contract ---

  it('duplicating a compute experiment prefills the launch contract', () => {
    mockFetchTaskTypeCatalog.mockResolvedValue([
      catalogEntry({
        id: 'compute_experiment',
        label: 'Compute Experiment',
        user_launchable: false,
        operator_contract_launchable: true,
      }),
    ]);
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    act(() => {
      result.current.openDuplicateTaskModal({
        id: 'task-ce-duplicate',
        title: 'Original compute experiment',
        description: 'Rerun this experiment',
        task_type: 'compute_experiment',
        payloads: [
          {
            github_repo: 'owner/repo',
            base_branch: 'main',
            command: 'python train.py --epochs 5',
            cwd: '/workspace/train',
            env: { BATCH_SIZE: '32', LR: '0.001' },
            accelerator: 'T4',
            machine_type: 'n1-standard-1',
            spend_cap_usd: 10.5,
            deadline_seconds: 3600,
            experiment: 'lora-sweep',
            benchmark: 'mmlu',
            hypothesis: 'Accuracy improves with LoRA',
            metric: 'accuracy',
            success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.85 },
            require_live_release: true,
            require_durable_artifact: false,
            provisioning_model: 'SPOT',
          },
        ] as unknown as Record<string, unknown>[],
      } as unknown as Parameters<typeof result.current.openDuplicateTaskModal>[0]);
    });
    // Verify modal opens with compute_experiment type
    expect(result.current.showNewTaskModal).toBe(true);
    expect(result.current.newTaskType).toBe('compute_experiment');
    // Verify compute experiment form is prefilled from the payload
    expect(result.current.computeExperiment.command).toBe('python train.py --epochs 5');
    expect(result.current.computeExperiment.cwd).toBe('/workspace/train');
    expect(result.current.computeExperiment.envVars).toBe('BATCH_SIZE=32\nLR=0.001');
    expect(result.current.computeExperiment.accelerator).toBe('T4');
    expect(result.current.computeExperiment.machineType).toBe('n1-standard-1');
    expect(result.current.computeExperiment.spendCapUsd).toBe('10.5');
    expect(result.current.computeExperiment.deadlineSeconds).toBe('3600');
    expect(result.current.computeExperiment.experiment).toBe('lora-sweep');
    expect(result.current.computeExperiment.benchmark).toBe('mmlu');
    expect(result.current.computeExperiment.hypothesis).toBe('Accuracy improves with LoRA');
    expect(result.current.computeExperiment.metric).toBe('accuracy');
    expect(result.current.computeExperiment.successMetric).toBe('accuracy');
    expect(result.current.computeExperiment.successOperator).toBe('>=');
    expect(result.current.computeExperiment.successThreshold).toBe('0.85');
    expect(result.current.computeExperiment.requireLiveRelease).toBe(true);
    expect(result.current.computeExperiment.requireDurableArtifact).toBe(false);
    expect(result.current.computeExperiment.provisioningModel).toBe('SPOT');
    // Verify general task fields are prefilled
    expect(result.current.newTaskRepo).toBe('owner/repo');
    expect(result.current.newTaskBranch).toBe('main');
    expect(result.current.newTaskTitle).toBe('Original compute experiment');
    expect(result.current.newTaskPrompt).toBe('Rerun this experiment');
  });

  // --- compute_experiment dedicated launch contract ---

  it('surfaces compute_experiment as launchable from an operator_contract_launchable catalog entry', async () => {
    mockFetchTaskTypeCatalog.mockResolvedValue([
      catalogEntry({ id: 'create', label: 'Create', description: 'Write code, open PR' }),
      catalogEntry({
        id: 'compute_experiment',
        label: 'Compute Experiment',
        description: 'Run a bounded experiment',
        user_launchable: false,
        operator_contract_launchable: true,
      }),
    ]);
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    await act(async () => {
      result.current.openNewTaskModal();
    });
    await waitFor(() => {
      const values = result.current.launchableTaskTypeOptions.map(o => o.value);
      expect(values).toContain('compute_experiment');
    });
  });

  it('handleCreateTask routes a compute_experiment to the dedicated endpoint with the contract body', async () => {
    const addToast = vi.fn();
    const fetchTasksRef = { current: vi.fn() };
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, fetchTasksRef),
    );
    act(() => {
      result.current.setNewTaskType('compute_experiment');
      result.current.setNewTaskRepo('owner/repo');
      result.current.setNewTaskBranch('main');
      result.current.setComputeExperiment(f => ({
        ...f,
        command: 'python train.py',
        accelerator: 'T4',
        machineType: 'n1-standard-1',
        spendCapUsd: '5',
        deadlineSeconds: '1800',
        hypothesis: 'accuracy improves',
        successMetric: 'accuracy',
        successOperator: '>=',
        successThreshold: '0.9',
      }));
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    // The generic task endpoint must NOT be hit for compute_experiment.
    expect(fetchSpy).not.toHaveBeenCalledWith(
      expect.stringContaining('/api/admin/tasks'),
      expect.anything(),
    );
    expect(mockCreateComputeExperiment).toHaveBeenCalledTimes(1);
    const body = mockCreateComputeExperiment.mock.calls[0][0];
    expect(body).toMatchObject({
      github_repo: 'owner/repo',
      base_branch: 'main',
      command: 'python train.py',
      spend_cap_usd: 5,
      deadline_seconds: 1800,
      accelerator: 'T4',
      machine_type: 'n1-standard-1',
      hypothesis: 'accuracy improves',
      success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
    });
    expect(addToast).toHaveBeenCalledWith('Compute experiment launched', 'success');
    expect(fetchTasksRef.current).toHaveBeenCalled();
  });

  it('handleCreateTask routes accelerator-only compute_experiment without machine_type', async () => {
    const addToast = vi.fn();
    const fetchTasksRef = { current: vi.fn() };
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, fetchTasksRef),
    );
    act(() => {
      result.current.setNewTaskType('compute_experiment');
      result.current.setNewTaskRepo('owner/repo');
      result.current.setNewTaskBranch('main');
      result.current.setComputeExperiment(f => ({
        ...f,
        command: 'python train.py',
        accelerator: 'T4',
        machineType: '',
        spendCapUsd: '5',
        deadlineSeconds: '1800',
      }));
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(mockCreateComputeExperiment).toHaveBeenCalledTimes(1);
    const body = mockCreateComputeExperiment.mock.calls[0][0];
    expect(body.accelerator).toBe('T4');
    expect(body).not.toHaveProperty('machine_type');
    expect(addToast).toHaveBeenCalledWith('Compute experiment launched', 'success');
    expect(fetchTasksRef.current).toHaveBeenCalled();
  });

  it('handleCreateTask rejects an explicit non-canonical GPU machine pairing', async () => {
    const addToast = vi.fn();
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, { current: vi.fn() }),
    );
    act(() => {
      result.current.setNewTaskType('compute_experiment');
      result.current.setComputeExperiment(f => ({
        ...f,
        command: 'python train.py',
        accelerator: 'T4',
        machineType: 'n1-standard-4',
        spendCapUsd: '5',
        deadlineSeconds: '1800',
      }));
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(addToast).toHaveBeenCalledWith('Machine type must match the selected accelerator canonical shape', 'error');
    expect(mockCreateComputeExperiment).not.toHaveBeenCalled();
  });

  it('handleCreateComputeExperiment shows held-for-approval toast when requires_approval is true', async () => {
    mockCreateComputeExperiment.mockResolvedValue({ id: 'ce-held', requires_approval: true, status: 'pending_approval' });
    const addToast = vi.fn();
    const fetchTasksRef = { current: vi.fn() };
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, fetchTasksRef),
    );
    act(() => {
      result.current.setNewTaskType('compute_experiment');
      result.current.setComputeExperiment(f => ({ ...f, command: 'python train.py', spendCapUsd: '5', deadlineSeconds: '1800' }));
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(addToast).toHaveBeenCalledWith(
      'Compute experiment held for approval — it will not run until an operator approves it',
      'success',
    );
    expect(addToast).not.toHaveBeenCalledWith('Compute experiment launched', 'success');
    expect(fetchTasksRef.current).toHaveBeenCalled();
  });

  it('handleCreateComputeExperiment shows duplicate toast when is_duplicate is true', async () => {
    mockCreateComputeExperiment.mockResolvedValue({ id: 'ce-dup', is_duplicate: true, requires_approval: false, status: 'running' });
    const addToast = vi.fn();
    const fetchTasksRef = { current: vi.fn() };
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, fetchTasksRef),
    );
    act(() => {
      result.current.setNewTaskType('compute_experiment');
      result.current.setComputeExperiment(f => ({ ...f, command: 'python train.py', spendCapUsd: '5', deadlineSeconds: '1800' }));
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(addToast).toHaveBeenCalledWith('Reusing an existing matching compute experiment', 'success');
    expect(addToast).not.toHaveBeenCalledWith('Compute experiment launched', 'success');
    expect(fetchTasksRef.current).toHaveBeenCalled();
  });

  it('handleCreateTask rejects a compute_experiment with an empty command', async () => {
    const addToast = vi.fn();
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, { current: vi.fn() }),
    );
    act(() => {
      result.current.setNewTaskType('compute_experiment');
      result.current.setComputeExperiment(f => ({ ...f, command: '   ', spendCapUsd: '5', deadlineSeconds: '1800' }));
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(addToast).toHaveBeenCalledWith('Command is required for compute experiments', 'error');
    expect(mockCreateComputeExperiment).not.toHaveBeenCalled();
  });

  it('handleCreateTask rejects a compute_experiment with a non-positive spend cap', async () => {
    const addToast = vi.fn();
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, { current: vi.fn() }),
    );
    act(() => {
      result.current.setNewTaskType('compute_experiment');
      result.current.setComputeExperiment(f => ({ ...f, command: 'python x.py', spendCapUsd: '0', deadlineSeconds: '1800' }));
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(addToast).toHaveBeenCalledWith('Spend cap must be a positive number (USD)', 'error');
    expect(mockCreateComputeExperiment).not.toHaveBeenCalled();
  });

  it('handleCreateTask rejects a compute_experiment with a non-positive deadline', async () => {
    const addToast = vi.fn();
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, { current: vi.fn() }),
    );
    act(() => {
      result.current.setNewTaskType('compute_experiment');
      result.current.setComputeExperiment(f => ({ ...f, command: 'python x.py', spendCapUsd: '5', deadlineSeconds: '-1' }));
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(addToast).toHaveBeenCalledWith('Deadline must be a positive integer (seconds)', 'error');
    expect(mockCreateComputeExperiment).not.toHaveBeenCalled();
  });

  it('handleCreateTask shows approval-hold toast for held compute_experiment responses', async () => {
    mockCreateComputeExperiment.mockResolvedValue({ id: 'task-held-1', requires_approval: true });
    const addToast = vi.fn();
    const fetchTasksRef = { current: vi.fn() };
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, fetchTasksRef),
    );
    act(() => {
      result.current.setNewTaskType('compute_experiment');
      result.current.setNewTaskRepo('owner/repo');
      result.current.setComputeExperiment(f => ({ ...f, command: 'python train.py', spendCapUsd: '500', deadlineSeconds: '1800' }));
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(addToast).toHaveBeenCalledWith(
      'Compute experiment held for approval — it will not run until an operator approves it',
      'success',
    );
    expect(addToast).not.toHaveBeenCalledWith('Compute experiment launched', 'success');
    expect(result.current.showNewTaskModal).toBe(false);
    expect(fetchTasksRef.current).toHaveBeenCalled();
  });

  it('handleCreateTask shows launched toast when requires_approval is false', async () => {
    mockCreateComputeExperiment.mockResolvedValue({ id: 'task-run-1', requires_approval: false });
    const addToast = vi.fn();
    const fetchTasksRef = { current: vi.fn() };
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, fetchTasksRef),
    );
    act(() => {
      result.current.setNewTaskType('compute_experiment');
      result.current.setNewTaskRepo('owner/repo');
      result.current.setComputeExperiment(f => ({ ...f, command: 'python train.py', spendCapUsd: '5', deadlineSeconds: '1800' }));
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(addToast).toHaveBeenCalledWith('Compute experiment launched', 'success');
    expect(result.current.showNewTaskModal).toBe(false);
    expect(fetchTasksRef.current).toHaveBeenCalled();
  });

  it('handleCreateTask surfaces a backend rejection from the dedicated compute endpoint', async () => {
    mockCreateComputeExperiment.mockRejectedValue(new Error('spend cap exceeds approval threshold'));
    const addToast = vi.fn();
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, { current: vi.fn() }),
    );
    act(() => {
      result.current.setNewTaskType('compute_experiment');
      result.current.setNewTaskRepo('owner/repo');
      result.current.setComputeExperiment(f => ({ ...f, command: 'python x.py', spendCapUsd: '500', deadlineSeconds: '1800' }));
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(addToast).toHaveBeenCalledWith(
      expect.stringContaining('spend cap exceeds approval threshold'),
      'error',
    );
  });

  it('handleCreateTask sends require_live_release when requireLiveRelease is set', async () => {
    const addToast = vi.fn();
    const fetchTasksRef = { current: vi.fn() };
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, fetchTasksRef),
    );
    act(() => {
      result.current.setNewTaskType('compute_experiment');
      result.current.setNewTaskRepo('owner/repo');
      result.current.setNewTaskBranch('main');
      result.current.setComputeExperiment(f => ({
        ...f,
        command: 'python train.py',
        spendCapUsd: '5',
        deadlineSeconds: '1800',
        requireLiveRelease: true,
      }));
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(mockCreateComputeExperiment).toHaveBeenCalledTimes(1);
    const body = mockCreateComputeExperiment.mock.calls[0][0];
    expect(body.require_live_release).toBe(true);
  });

  it('handleCreateTask omits require_live_release when requireLiveRelease is false', async () => {
    const addToast = vi.fn();
    const fetchTasksRef = { current: vi.fn() };
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, fetchTasksRef),
    );
    act(() => {
      result.current.setNewTaskType('compute_experiment');
      result.current.setNewTaskRepo('owner/repo');
      result.current.setNewTaskBranch('main');
      result.current.setComputeExperiment(f => ({
        ...f,
        command: 'python train.py',
        spendCapUsd: '5',
        deadlineSeconds: '1800',
      }));
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(mockCreateComputeExperiment).toHaveBeenCalledTimes(1);
    const body = mockCreateComputeExperiment.mock.calls[0][0];
    expect(body).not.toHaveProperty('require_live_release');
  });

  it('handleCreateTask sends require_durable_artifact when requireDurableArtifact is set', async () => {
    const addToast = vi.fn();
    const fetchTasksRef = { current: vi.fn() };
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, fetchTasksRef),
    );
    act(() => {
      result.current.setNewTaskType('compute_experiment');
      result.current.setNewTaskRepo('owner/repo');
      result.current.setNewTaskBranch('main');
      result.current.setComputeExperiment(f => ({
        ...f,
        command: 'python train.py',
        spendCapUsd: '5',
        deadlineSeconds: '1800',
        requireDurableArtifact: true,
      }));
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(mockCreateComputeExperiment).toHaveBeenCalledTimes(1);
    const body = mockCreateComputeExperiment.mock.calls[0][0];
    expect(body.require_durable_artifact).toBe(true);
  });

  it('handleCreateTask omits require_durable_artifact when requireDurableArtifact is false', async () => {
    const addToast = vi.fn();
    const fetchTasksRef = { current: vi.fn() };
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, fetchTasksRef),
    );
    act(() => {
      result.current.setNewTaskType('compute_experiment');
      result.current.setNewTaskRepo('owner/repo');
      result.current.setNewTaskBranch('main');
      result.current.setComputeExperiment(f => ({
        ...f,
        command: 'python train.py',
        spendCapUsd: '5',
        deadlineSeconds: '1800',
      }));
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(mockCreateComputeExperiment).toHaveBeenCalledTimes(1);
    const body = mockCreateComputeExperiment.mock.calls[0][0];
    expect(body).not.toHaveProperty('require_durable_artifact');
  });

  it('handleCreateTask sends cwd and parsed env when set', async () => {
    const addToast = vi.fn();
    const fetchTasksRef = { current: vi.fn() };
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, fetchTasksRef),
    );
    act(() => {
      result.current.setNewTaskType('compute_experiment');
      result.current.setNewTaskRepo('owner/repo');
      result.current.setNewTaskBranch('main');
      result.current.setComputeExperiment(f => ({
        ...f,
        command: 'python train.py',
        spendCapUsd: '5',
        deadlineSeconds: '1800',
        cwd: '/workspace/project',
        envVars: 'BATCH_SIZE=32\nLR=0.001\n# comment\n\nDEBUG=true',
      }));
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(mockCreateComputeExperiment).toHaveBeenCalledTimes(1);
    const body = mockCreateComputeExperiment.mock.calls[0][0];
    expect(body.cwd).toBe('/workspace/project');
    expect(body.env).toEqual({ BATCH_SIZE: '32', LR: '0.001', DEBUG: 'true' });
  });

  it('handleCreateTask omits cwd and env when blank', async () => {
    const addToast = vi.fn();
    const fetchTasksRef = { current: vi.fn() };
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, fetchTasksRef),
    );
    act(() => {
      result.current.setNewTaskType('compute_experiment');
      result.current.setNewTaskRepo('owner/repo');
      result.current.setNewTaskBranch('main');
      result.current.setComputeExperiment(f => ({
        ...f,
        command: 'python train.py',
        spendCapUsd: '5',
        deadlineSeconds: '1800',
      }));
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(mockCreateComputeExperiment).toHaveBeenCalledTimes(1);
    const body = mockCreateComputeExperiment.mock.calls[0][0];
    expect(body).not.toHaveProperty('cwd');
    expect(body).not.toHaveProperty('env');
  });

  it('handleCreateTask sends container_image and container_registry_auth when set', async () => {
    const addToast = vi.fn();
    const fetchTasksRef = { current: vi.fn() };
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, fetchTasksRef),
    );
    act(() => {
      result.current.setNewTaskType('compute_experiment');
      result.current.setNewTaskRepo('owner/repo');
      result.current.setNewTaskBranch('main');
      result.current.setComputeExperiment(f => ({
        ...f,
        command: 'python train.py',
        spendCapUsd: '5',
        deadlineSeconds: '1800',
        containerImage: 'nvcr.io/nvidia/tensorflow:25.01',
        containerRegistryServer: 'nvcr.io',
        containerRegistryUsername: '$oauthtoken',
        containerRegistryPassword: 'ngc-secret-key',
      }));
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(mockCreateComputeExperiment).toHaveBeenCalledTimes(1);
    const body = mockCreateComputeExperiment.mock.calls[0][0];
    expect(body.container_image).toBe('nvcr.io/nvidia/tensorflow:25.01');
    expect(body.container_registry_auth).toEqual({
      server: 'nvcr.io',
      username: '$oauthtoken',
      password: 'ngc-secret-key',
    });
  });

  it('handleCreateTask sends container_image bare (no auth) when only the image is set', async () => {
    const addToast = vi.fn();
    const fetchTasksRef = { current: vi.fn() };
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, fetchTasksRef),
    );
    act(() => {
      result.current.setNewTaskType('compute_experiment');
      result.current.setNewTaskRepo('owner/repo');
      result.current.setNewTaskBranch('main');
      result.current.setComputeExperiment(f => ({
        ...f,
        command: 'python train.py',
        spendCapUsd: '5',
        deadlineSeconds: '1800',
        containerImage: 'nvcr.io/nvidia/tensorflow:25.01',
      }));
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(mockCreateComputeExperiment).toHaveBeenCalledTimes(1);
    const body = mockCreateComputeExperiment.mock.calls[0][0];
    expect(body.container_image).toBe('nvcr.io/nvidia/tensorflow:25.01');
    expect(body).not.toHaveProperty('container_registry_auth');
  });

  it('handleCreateTask rejects a registry password without server/username', async () => {
    const addToast = vi.fn();
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, { current: vi.fn() }),
    );
    act(() => {
      result.current.setNewTaskType('compute_experiment');
      result.current.setNewTaskRepo('owner/repo');
      result.current.setComputeExperiment(f => ({
        ...f,
        command: 'python train.py',
        spendCapUsd: '5',
        deadlineSeconds: '1800',
        containerImage: 'nvcr.io/nvidia/tensorflow:25.01',
        containerRegistryPassword: 'ngc-secret-key',
      }));
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(addToast).toHaveBeenCalledWith(
      'Registry credentials need a server and username when a password is set',
      'error',
    );
    expect(mockCreateComputeExperiment).not.toHaveBeenCalled();
  });

  it('openDuplicateTaskModal prefills container image + registry server/username but never the password', () => {
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    act(() => {
      result.current.openDuplicateTaskModal({
        id: 'task-ce-container',
        title: 'Containerized experiment',
        description: 'rerun it',
        task_type: 'compute_experiment',
        payloads: [
          {
            github_repo: 'owner/repo',
            base_branch: 'main',
            command: 'python train.py',
            spend_cap_usd: 5,
            deadline_seconds: 1800,
            container_image: 'nvcr.io/nvidia/tensorflow:25.01',
            container_registry_auth: { server: 'nvcr.io', username: '$oauthtoken', password: 'should-not-leak' },
          },
        ] as unknown as Record<string, unknown>[],
      } as unknown as Parameters<typeof result.current.openDuplicateTaskModal>[0]);
    });
    expect(result.current.computeExperiment.containerImage).toBe('nvcr.io/nvidia/tensorflow:25.01');
    expect(result.current.computeExperiment.containerRegistryServer).toBe('nvcr.io');
    expect(result.current.computeExperiment.containerRegistryUsername).toBe('$oauthtoken');
    // Secret must never be prefilled from a stored payload.
    expect(result.current.computeExperiment.containerRegistryPassword).toBe('');
  });

  // --- runner_target_id / force-onto-runner ---

  it('handleCreateTask threads runner_target_id into the compute body when forcedRunnerId is set', async () => {
    const addToast = vi.fn();
    const fetchTasksRef = { current: vi.fn() };
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, fetchTasksRef),
    );
    act(() => {
      result.current.setNewTaskType('compute_experiment');
      result.current.setNewTaskRepo('owner/repo');
      result.current.setNewTaskBranch('main');
      result.current.setComputeExperiment(f => ({
        ...f,
        command: 'python train.py',
        spendCapUsd: '5',
        deadlineSeconds: '1800',
        forcedRunnerId: 'runner-uuid-dgx1',
      }));
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(mockCreateComputeExperiment).toHaveBeenCalledTimes(1);
    const body = mockCreateComputeExperiment.mock.calls[0][0];
    expect(body.runner_target_id).toBe('runner-uuid-dgx1');
  });

  it('handleCreateTask omits runner_target_id when forcedRunnerId is empty', async () => {
    const addToast = vi.fn();
    const fetchTasksRef = { current: vi.fn() };
    const { result } = renderHook(() =>
      useTaskModal('create', addToast, fetchTasksRef),
    );
    act(() => {
      result.current.setNewTaskType('compute_experiment');
      result.current.setNewTaskRepo('owner/repo');
      result.current.setNewTaskBranch('main');
      result.current.setComputeExperiment(f => ({
        ...f,
        command: 'python train.py',
        spendCapUsd: '5',
        deadlineSeconds: '1800',
        forcedRunnerId: '',
      }));
    });
    await act(async () => {
      await result.current.handleCreateTask({ preventDefault: vi.fn() } as unknown as React.FormEvent);
    });
    expect(mockCreateComputeExperiment).toHaveBeenCalledTimes(1);
    const body = mockCreateComputeExperiment.mock.calls[0][0];
    expect(body).not.toHaveProperty('runner_target_id');
  });

  it('openDuplicateTaskModal leaves forcedRunnerId empty — forced pin is not re-applied on clone', () => {
    const { result } = renderHook(() =>
      useTaskModal('create', vi.fn(), { current: vi.fn() }),
    );
    act(() => {
      result.current.openDuplicateTaskModal({
        id: 'task-ce-forced',
        title: 'Forced experiment',
        description: 'rerun it',
        task_type: 'compute_experiment',
        payloads: [
          {
            github_repo: 'owner/repo',
            base_branch: 'main',
            command: 'python train.py',
            spend_cap_usd: 5,
            deadline_seconds: 1800,
            runner_target_id: 'runner-uuid-dgx1',
          },
        ] as unknown as Record<string, unknown>[],
      } as unknown as Parameters<typeof result.current.openDuplicateTaskModal>[0]);
    });
    // The forced runner target from the original task must never be carried over
    // to a clone — the operator must choose a target for each new launch.
    expect(result.current.computeExperiment.forcedRunnerId).toBe('');
  });
});
