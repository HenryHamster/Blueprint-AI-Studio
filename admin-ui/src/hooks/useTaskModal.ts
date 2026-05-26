import { useEffect, useState } from 'react';
import type React from 'react';
import { API_BASE_URL, authHeaders, authGet } from '../api';
import type { UserLaunchableTaskType } from '../taskCapabilities';

interface RepoOption {
  full_name: string;
  default_branch: string;
}

export function useTaskModal(
  defaultTaskType: UserLaunchableTaskType,
  addToast: (msg: string, type: 'success' | 'error') => void,
  fetchTasksRef: React.MutableRefObject<() => void>,
  defaultTaskBranchRef?: React.MutableRefObject<string>,
  selectedProjectRepoRef?: React.MutableRefObject<string | null>,
) {
  const getDefaultTaskBranch = () => defaultTaskBranchRef?.current || 'main';
  const getSelectedProjectRepo = () => selectedProjectRepoRef?.current || null;
  const defaultTaskBranch = getDefaultTaskBranch();

  // Modal visibility
  const [showNewTaskModal, setShowNewTaskModal] = useState(false);

  // Form fields
  const [newTaskRepo, setNewTaskRepo] = useState('');
  const [newTaskBranch, setNewTaskBranch] = useState(defaultTaskBranch);
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

  useEffect(() => {
    if (!showNewTaskModal) {
      setNewTaskBranch(defaultTaskBranch);
    }
  }, [defaultTaskBranch, showNewTaskModal]);

  const getDefaultBranchForRepo = (repo: string) => {
    const repoMeta = repoOptions.find(r => r.full_name === repo);
    const selectedProjectRepo = getSelectedProjectRepo();
    const defaultTaskBranch = getDefaultTaskBranch();
    if (repo && repo === selectedProjectRepo) {
      return defaultTaskBranch || repoMeta?.default_branch || 'main';
    }
    return repoMeta?.default_branch || defaultTaskBranch || 'main';
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

  const openNewTaskModal = () => {
    setShowNewTaskModal(true);
    setNewTaskType(defaultTaskType);
    setNewTaskModel(getDefaultModelForType(defaultTaskType));
    setNewTaskEffort(getDefaultEffortForType(defaultTaskType));
    setNewTaskBranch(getDefaultTaskBranch());
    setUseManualRepo(false);
    setUseManualBranch(false);
    setBranchOptions([]);
    setBranchError(null);
    fetchRepoOptions();
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
    if (newTaskType === 'review' || newTaskType === 'fix') {
      const prNum = parseInt(newTaskPrNumber, 10);
      if (!newTaskPrNumber || isNaN(prNum) || prNum < 1) {
        addToast('PR number must be a positive integer', 'error');
        return;
      }
      if (newTaskType === 'fix' && !newTaskPrBranch) {
        addToast('PR branch is required for fix tasks', 'error');
        return;
      }
    }
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks`, {
        method: 'POST',
        headers: await authHeaders(),
        body: JSON.stringify({
          github_repo: newTaskRepo,
          title: newTaskTitle,
          prompt: newTaskPrompt,
          base_branch: newTaskBranch,
          task_type: newTaskType,
          ...(newTaskType === 'review' || newTaskType === 'fix' ? {
            pr_number: parseInt(newTaskPrNumber, 10),
            branch: newTaskPrBranch || undefined,
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
      addToast('Task created successfully', 'success');
      fetchTasksRef.current();
    } catch (error) {
      console.error('Failed to create task:', error);
      addToast('Failed to create task', 'error');
    }
  };

  return {
    showNewTaskModal, setShowNewTaskModal,
    newTaskRepo, newTaskBranch, newTaskTitle, newTaskPrompt, newTaskType,
    newTaskPrNumber, newTaskPrBranch, newTaskModel, newTaskEffort,
    setNewTaskRepo, setNewTaskBranch, setNewTaskTitle, setNewTaskPrompt,
    setNewTaskType, setNewTaskPrNumber, setNewTaskPrBranch, setNewTaskModel, setNewTaskEffort,
    repoOptions, repoLoading, repoError, useManualRepo, setUseManualRepo,
    branchOptions, branchLoading, branchError, useManualBranch, setUseManualBranch, setBranchOptions,
    openNewTaskModal, handleRepoChange, handleCreateTask, getDefaultModelForType, getDefaultEffortForType,
  };
}
