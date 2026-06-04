import { useEffect, useState } from 'react';
import type React from 'react';
import { API_BASE_URL, authHeaders, authGet, fetchTaskTypeCatalog } from '../api';
import type { LaunchableTaskTypeOption, UserLaunchableTaskType } from '../taskCapabilities';
import { USER_LAUNCHABLE_TASK_TYPE_IDS, USER_LAUNCHABLE_TASK_TYPE_OPTIONS, resolveTaskCapabilities } from '../taskCapabilities';
import type { TaskCapabilityContract } from '../types/settings';
import type { Task } from '../types/task';

interface RepoOption {
  full_name: string;
  default_branch: string;
}

export function useTaskModal(
  defaultTaskType: UserLaunchableTaskType,
  addToast: (msg: string, type: 'success' | 'error') => void,
  fetchTasksRef: React.MutableRefObject<() => void>,
  defaultTaskBranchRef?: React.MutableRefObject<string | null>,
  selectedProjectRepoRef?: React.MutableRefObject<string | null>,
  taskCapabilityContract?: TaskCapabilityContract | null,
) {
  // Launchable set derives from the published contract (falling back to the
  // built-in table when the backend hasn't published it) so the duplicate
  // guard below honors a newly registered launchable type instead of
  // collapsing it to 'create'.
  const launchableTaskTypeIds: readonly string[] = taskCapabilityContract
    ? resolveTaskCapabilities(taskCapabilityContract).userLaunchableOptions.map(o => o.value)
    : USER_LAUNCHABLE_TASK_TYPE_IDS;
  const getDefaultTaskBranch = () => defaultTaskBranchRef?.current ?? null;
  const getSelectedProjectRepo = () => selectedProjectRepoRef?.current || null;
  const defaultTaskBranch = getDefaultTaskBranch();

  // Modal visibility
  const [showNewTaskModal, setShowNewTaskModal] = useState(false);
  // Tracks which opener populated the modal — 'new' persists drafts, 'duplicate' suppresses
  // them so prefilled source-task content doesn't overwrite the user's saved draft.
  const [modalOpener, setModalOpener] = useState<'new' | 'duplicate'>('new');

  // Form fields
  const [newTaskRepo, setNewTaskRepo] = useState('');
  const [newTaskBranch, setNewTaskBranch] = useState(defaultTaskBranch ?? '');
  const [newTaskTitle, setNewTaskTitle] = useState('');
  const [newTaskPrompt, setNewTaskPrompt] = useState('');
  const [newTaskType, setNewTaskType] = useState<UserLaunchableTaskType>('create');
  const [newTaskPrNumber, setNewTaskPrNumber] = useState('');
  const [newTaskPrBranch, setNewTaskPrBranch] = useState('');
  const [newTaskModel, setNewTaskModel] = useState('');
  const [newTaskEffort, setNewTaskEffort] = useState('');

  // GitHub repo/branch dropdown state
  const [repoOptions, setRepoOptions] = useState<RepoOption[]>([]);
  const [repoLoading, setRepoLoading] = useState(false);
  const [repoError, setRepoError] = useState<string | null>(null);
  const [useManualRepo, setUseManualRepo] = useState(false);
  const [branchOptions, setBranchOptions] = useState<string[]>([]);
  const [branchLoading, setBranchLoading] = useState(false);
  const [branchError, setBranchError] = useState<string | null>(null);
  const [useManualBranch, setUseManualBranch] = useState(false);

  // Launchable task-type options, derived from the backend task-type catalog so
  // a newly registered launchable type appears in the launcher with no frontend
  // edit. Seeded with (and falling back to) the static list so the dropdown is
  // never empty while the catalog loads or if the fetch fails.
  const [launchableTaskTypeOptions, setLaunchableTaskTypeOptions] =
    useState<LaunchableTaskTypeOption[]>(() => [...USER_LAUNCHABLE_TASK_TYPE_OPTIONS]);

  const fetchLaunchableTaskTypeOptions = async () => {
    try {
      const catalog = await fetchTaskTypeCatalog();
      const launchable = catalog
        .filter(entry => entry.user_launchable)
        .map(entry => ({ value: entry.id, label: entry.label, description: entry.description }));
      // Only adopt the catalog when it yields options; an empty/garbled
      // response must not blank the dropdown — keep the static fallback.
      if (launchable.length > 0) {
        setLaunchableTaskTypeOptions(launchable);
      }
    } catch {
      // Catalog unreachable — retain the current (static) fallback options.
    }
  };

  useEffect(() => {
    if (!showNewTaskModal) {
      setNewTaskBranch(defaultTaskBranch ?? '');
    }
  }, [defaultTaskBranch, showNewTaskModal]);

  useEffect(() => {
    if (showNewTaskModal && defaultTaskBranch && !newTaskBranch) {
      setNewTaskBranch(defaultTaskBranch);
    }
  }, [defaultTaskBranch, newTaskBranch, showNewTaskModal]);

  const getDefaultBranchForRepo = (repo: string) => {
    const repoMeta = repoOptions.find(r => r.full_name === repo);
    const selectedProjectRepo = getSelectedProjectRepo();
    const defaultTaskBranch = getDefaultTaskBranch();
    if (repo && repo === selectedProjectRepo) {
      return defaultTaskBranch ?? '';
    }
    return repoMeta?.default_branch || defaultTaskBranch || '';
  };

  const getDefaultModelForType = (taskType: string) => {
    return JSON.parse(localStorage.getItem(`blueprint-admin:taskTypeModel:${taskType}`) ?? 'null') ??
      JSON.parse(localStorage.getItem('blueprint-admin:defaultModel') ?? 'null') ?? '';
  };

  const getDefaultEffortForType = (taskType: string) => {
    return JSON.parse(localStorage.getItem(`blueprint-admin:taskTypeEffort:${taskType}`) ?? 'null') ??
      JSON.parse(localStorage.getItem('blueprint-admin:defaultEffort') ?? 'null') ?? '';
  };

  const fetchRepoOptions = async () => {
    setRepoLoading(true);
    setRepoError(null);
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/github/repos`);
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        setRepoError(errData?.detail || 'Failed to load repositories');
        setUseManualRepo(true);
        return;
      }
      const data = await res.json();
      setRepoOptions(data.repositories || []);
      if (data.repositories?.length === 0) {
        setRepoError('No repositories found for this GitHub installation');
        setUseManualRepo(true);
      }
    } catch {
      setRepoError('Failed to connect to server');
      setUseManualRepo(true);
    } finally {
      setRepoLoading(false);
    }
  };

  const fetchBranchOptions = async (repo: string) => {
    setBranchOptions([]);
    setBranchError(null);
    setUseManualBranch(false);
    if (!repo) return;
    setBranchLoading(true);
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/github/branches?repo=${encodeURIComponent(repo)}`);
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        setBranchError(errData?.detail || 'Failed to load branches');
        setUseManualBranch(true);
        return;
      }
      const data = await res.json();
      setBranchOptions(data.branches || []);
      if (data.branches?.length === 0) {
        setBranchError('No branches found');
        setUseManualBranch(true);
      }
    } catch {
      setBranchError('Failed to connect to server');
      setUseManualBranch(true);
    } finally {
      setBranchLoading(false);
    }
  };

  const getDraftStorageKey = (suffix: string) => {
    const projectRepo = getSelectedProjectRepo();
    return projectRepo ? `blueprint-admin:draft:${projectRepo}:${suffix}` : null;
  };

  const restoreDraft = () => {
    const titleKey = getDraftStorageKey('title');
    const promptKey = getDraftStorageKey('prompt');
    if (!titleKey || !promptKey) return;
    const savedTitle = localStorage.getItem(titleKey);
    const savedPrompt = localStorage.getItem(promptKey);
    if (savedTitle) setNewTaskTitle(savedTitle);
    if (savedPrompt) setNewTaskPrompt(savedPrompt);
  };

  const clearDraft = () => {
    const titleKey = getDraftStorageKey('title');
    const promptKey = getDraftStorageKey('prompt');
    if (!titleKey || !promptKey) return;
    localStorage.removeItem(titleKey);
    localStorage.removeItem(promptKey);
  };

  useEffect(() => {
    // Only persist drafts while the new-task modal is the active opener.
    // When opened as a duplicate, the title/prompt are prefilled from the source task,
    // and persisting them would clobber the user's saved draft (issue #6321 review).
    if (!showNewTaskModal || modalOpener !== 'new') return;
    const projectRepo = selectedProjectRepoRef?.current || null;
    if (!projectRepo) return;
    const timer = setTimeout(() => {
      const titleKey = `blueprint-admin:draft:${projectRepo}:title`;
      const promptKey = `blueprint-admin:draft:${projectRepo}:prompt`;
      localStorage.setItem(titleKey, newTaskTitle);
      localStorage.setItem(promptKey, newTaskPrompt);
    }, 500);
    return () => clearTimeout(timer);
  }, [newTaskTitle, newTaskPrompt, showNewTaskModal, modalOpener, selectedProjectRepoRef]);

  const openNewTaskModal = () => {
    setModalOpener('new');
    setShowNewTaskModal(true);
    setNewTaskType(defaultTaskType);
    setNewTaskModel(getDefaultModelForType(defaultTaskType));
    setNewTaskEffort(getDefaultEffortForType(defaultTaskType));
    setNewTaskBranch(getDefaultTaskBranch() ?? '');
    setUseManualRepo(false);
    setUseManualBranch(false);
    setBranchOptions([]);
    setBranchError(null);
    restoreDraft();
    fetchRepoOptions();
    fetchLaunchableTaskTypeOptions();
  };

  const handleRepoChange = (value: string) => {
    setNewTaskRepo(value);
    setNewTaskBranch(getDefaultBranchForRepo(value));
    if (value && value.includes('/')) {
      fetchBranchOptions(value);
    } else {
      setBranchOptions([]);
    }
  };

  const handleCreateTask = async (e: React.FormEvent) => {
    e.preventDefault();
    const normalizedPrBranch = newTaskPrBranch.trim();
    if (newTaskType === 'review' || newTaskType === 'panel_review' || newTaskType === 'fix') {
      const prNum = parseInt(newTaskPrNumber, 10);
      if (!newTaskPrNumber || isNaN(prNum) || prNum < 1) {
        addToast('PR number must be a positive integer', 'error');
        return;
      }
      if (!normalizedPrBranch) {
        const taskLabel = newTaskType === 'fix'
          ? 'fix'
          : newTaskType === 'panel_review'
            ? 'panel review'
            : 'review';
        addToast(`PR branch is required for ${taskLabel} tasks`, 'error');
        return;
      }
    }
    // panel_review runs a FIXED multi-lens workflow whose driving prompt is
    // built by the handler; an operator-authored title/prompt is not required.
    // The backend still mandates non-empty title/prompt (min_length=1), so fill
    // sensible defaults from the PR context when the operator left them blank.
    const isPanelReview = newTaskType === 'panel_review';
    const effectiveTitle = newTaskTitle.trim() || (isPanelReview
      ? `Panel review PR #${newTaskPrNumber || '?'}`
      : newTaskTitle);
    const effectivePrompt = newTaskPrompt.trim() || (isPanelReview
      ? `Run the fixed multi-agent panel review on PR #${newTaskPrNumber || '?'}.`
      : newTaskPrompt);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks`, {
        method: 'POST',
        headers: await authHeaders(),
        body: JSON.stringify({
          github_repo: newTaskRepo,
          title: effectiveTitle,
          prompt: effectivePrompt,
          base_branch: newTaskBranch,
          task_type: newTaskType,
          ...(newTaskType === 'review' || newTaskType === 'panel_review' || newTaskType === 'fix' ? {
            pr_number: parseInt(newTaskPrNumber, 10),
            branch: normalizedPrBranch || undefined,
          } : {}),
          ...(newTaskModel ? { model: newTaskModel } : {}),
          ...(newTaskEffort ? { effort: newTaskEffort } : {}),
        }),
      });
      if (!res.ok) {
        const errData = await res.json();
        addToast(`Failed to create task: ${errData?.detail || errData?.error?.message || res.statusText}`, 'error');
        return;
      }
      setShowNewTaskModal(false);
      setNewTaskTitle('');
      setNewTaskPrompt('');
      setNewTaskPrNumber('');
      setNewTaskPrBranch('');
      clearDraft();
      addToast('Task created successfully', 'success');
      fetchTasksRef.current();
    } catch (error) {
      console.error('Failed to create task:', error);
      addToast('Failed to create task', 'error');
    }
  };

  const openPrefilledTaskModal = ({
    taskType,
    title,
    prompt,
    prNumber,
    prBranch,
    baseBranch,
  }: {
    taskType: UserLaunchableTaskType;
    title: string;
    prompt: string;
    prNumber?: number | string;
    prBranch?: string;
    baseBranch?: string;
  }) => {
    const repo = getSelectedProjectRepo() || '';
    setModalOpener('duplicate');
    setShowNewTaskModal(true);
    setNewTaskRepo(repo);
    // Prefer the PR's own base branch (so a panel_review launched from a PR
    // grades against the right base); fall back to the project default.
    setNewTaskBranch(baseBranch || getDefaultTaskBranch() || '');
    setNewTaskTitle(title);
    setNewTaskPrompt(prompt);
    setNewTaskType(taskType);
    setNewTaskModel(getDefaultModelForType(taskType));
    setNewTaskEffort(getDefaultEffortForType(taskType));
    setNewTaskPrNumber(prNumber === undefined || prNumber === null ? '' : String(prNumber));
    setNewTaskPrBranch(prBranch ?? '');
    setUseManualRepo(false);
    setUseManualBranch(false);
    setBranchOptions([]);
    setBranchError(null);
    fetchRepoOptions();
    fetchLaunchableTaskTypeOptions();
    if (repo) fetchBranchOptions(repo);
  };

  const openDuplicateTaskModal = (task: Task) => {
    const payload = task.payloads?.[0] ?? {};
    const repo = (payload.github_repo as string) || '';
    const branch = (payload.base_branch as string) || '';
    const model = (payload.model as string) || '';
    const effort = (payload.effort as string) || '';
    const taskType: UserLaunchableTaskType =
      task.task_type && launchableTaskTypeIds.includes(task.task_type)
        ? (task.task_type as UserLaunchableTaskType)
        : 'create';

    setModalOpener('duplicate');
    setShowNewTaskModal(true);
    setNewTaskRepo(repo);
    setNewTaskBranch(branch || getDefaultTaskBranch() || '');
    setNewTaskTitle(task.title);
    setNewTaskPrompt(task.description || '');
    setNewTaskType(taskType);
    setNewTaskModel(model || getDefaultModelForType(taskType));
    setNewTaskEffort(effort || getDefaultEffortForType(taskType));
    setNewTaskPrNumber('');
    setNewTaskPrBranch('');
    setUseManualRepo(false);
    setUseManualBranch(false);
    setBranchOptions([]);
    setBranchError(null);
    fetchRepoOptions();
    fetchLaunchableTaskTypeOptions();
    if (repo) fetchBranchOptions(repo);
  };

  return {
    showNewTaskModal, setShowNewTaskModal,
    newTaskRepo, newTaskBranch, newTaskTitle, newTaskPrompt, newTaskType,
    newTaskPrNumber, newTaskPrBranch, newTaskModel, newTaskEffort,
    setNewTaskRepo, setNewTaskBranch, setNewTaskTitle, setNewTaskPrompt,
    setNewTaskType, setNewTaskPrNumber, setNewTaskPrBranch, setNewTaskModel, setNewTaskEffort,
    repoOptions, repoLoading, repoError, useManualRepo, setUseManualRepo,
    branchOptions, branchLoading, branchError, useManualBranch, setUseManualBranch, setBranchOptions,
    launchableTaskTypeOptions,
    openNewTaskModal, openDuplicateTaskModal, openPrefilledTaskModal, handleRepoChange, handleCreateTask, getDefaultModelForType, getDefaultEffortForType,
  };
}
