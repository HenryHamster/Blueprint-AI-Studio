import { useState, useEffect, useRef, useCallback } from 'react';
import type { Task } from './components/TaskCard';
import type { Project } from './components/ProjectDashboard';
import type { MaxPlanAccount } from './components/SettingsDropdown';
import { onAuthChange } from './firebase';
import type { User } from 'firebase/auth';
import { API_BASE_URL, authHeaders, authGet } from './api';
import { useToasts } from './components/Toasts';

function usePersistedState<T>(key: string, defaultValue: T): [T, React.Dispatch<React.SetStateAction<T>>] {
  const [state, setState] = useState<T>(() => {
    try {
      const stored = localStorage.getItem(`blueprint-admin:${key}`);
      return stored ? JSON.parse(stored) : defaultValue;
    } catch { return defaultValue; }
  });
  useEffect(() => {
    localStorage.setItem(`blueprint-admin:${key}`, JSON.stringify(state));
  }, [key, state]);
  return [state, setState];
}

export function useAppState() {
  const [user, setUser] = useState<User | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const limit = 50;

  // New Task modal state
  const [showNewTaskModal, setShowNewTaskModal] = useState(false);
  const [newTaskRepo, setNewTaskRepo] = useState('');
  const [newTaskBranch, setNewTaskBranch] = useState('main');
  const [newTaskTitle, setNewTaskTitle] = useState('');
  const [newTaskPrompt, setNewTaskPrompt] = useState('');
  const [newTaskType, setNewTaskType] = useState<'create' | 'plan' | 'research' | 'review' | 'fix'>('create');
  const [newTaskPrNumber, setNewTaskPrNumber] = useState('');
  const [newTaskPrBranch, setNewTaskPrBranch] = useState('');

  // GitHub repo/branch dropdown state
  interface RepoOption { full_name: string; default_branch: string; }
  const [repoOptions, setRepoOptions] = useState<RepoOption[]>([]);
  const [repoLoading, setRepoLoading] = useState(false);
  const [repoError, setRepoError] = useState<string | null>(null);
  const [useManualRepo, setUseManualRepo] = useState(false);
  const [branchOptions, setBranchOptions] = useState<string[]>([]);
  const [branchLoading, setBranchLoading] = useState(false);
  const [branchError, setBranchError] = useState<string | null>(null);
  const [useManualBranch, setUseManualBranch] = useState(false);
  const [actionLoadingMap, setActionLoadingMap] = useState<Record<string, string>>({});
  const { toasts, addToast, dismissToast } = useToasts();

  // Log viewer state
  const [logTask, setLogTask] = useState<Task | null>(null);
  const [logLines, setLogLines] = useState<string[]>([]);
  const wsRef = useRef<WebSocket | null>(null);

  // Research browser state
  const [showResearchBrowser, setShowResearchBrowser] = useState(false);

  // Settings state
  const [showSettings, setShowSettings] = useState(false);
  const [maxConcurrentVMs, setMaxConcurrentVMs] = useState(5);
  const [maxRetries, setMaxRetries] = useState(3);
  const [usingMaxPlan, setUsingMaxPlan] = useState(false);
  const settingsRef = useRef<HTMLDivElement>(null);
  const fetchTasksDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Max plan pool state
  const [maxPlanAccounts, setMaxPlanAccounts] = useState<MaxPlanAccount[]>([]);
  const [newAccountName, setNewAccountName] = useState('');
  const [newAccountCreds, setNewAccountCreds] = useState('');

  // GitHub connection state
  const [githubAccount, setGithubAccount] = useState<string | null>(null);

  // View toggle state
  const [viewMode, setViewMode] = usePersistedState<'kanban' | 'pipeline'>('viewMode', 'kanban');

  // Project dashboard state
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProjectId, setSelectedProjectId] = usePersistedState<string | null>('selectedProject', null);

  // Directions panel state
  const [showDirections, setShowDirections] = useState(false);

  // Refresh indicator state
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

  // Search and filter state
  const [searchQuery, setSearchQuery] = useState('');
  const [taskTypeFilter, setTaskTypeFilter] = useState('all');

  // Keyboard shortcut help state
  const [showShortcutHelp, setShowShortcutHelp] = useState(false);
  const shortcutHelpRef = useRef<HTMLDivElement>(null);

  // Firebase auth state
  useEffect(() => {
    const unsub = onAuthChange((u) => { setUser(u); setAuthLoading(false); });
    return unsub;
  }, []);

  // Handle OAuth redirect query params
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const connected = params.get('github_connected');
    const ghError = params.get('github_error');
    if (connected) {
      setError(null);
      setGithubAccount(connected);
      window.history.replaceState({}, '', window.location.pathname);
    } else if (ghError) {
      const messages: Record<string, string> = {
        state_expired: 'GitHub OAuth expired — try again',
        invalid_state: 'GitHub OAuth failed — invalid state',
        no_installations: 'No GitHub App installations found — install the app on your repos first',
        failed: 'GitHub connection failed — try again',
      };
      setError(messages[ghError] || `GitHub error: ${ghError}`);
      window.history.replaceState({}, '', window.location.pathname);
    }
  }, []);

  useEffect(() => {
    if (!user) return;
    fetchTasks();
    fetchSettings();
    fetchProjects();
    const interval = setInterval(fetchTasks, 30000);
    const projectInterval = setInterval(fetchProjects, 60000);
    return () => { clearInterval(interval); clearInterval(projectInterval); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, user]);

  // Clean up WebSocket on close
  useEffect(() => {
    return () => {
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, []);

  // Close settings dropdown on outside click
  useEffect(() => {
    if (!showSettings) return;
    const handleClick = (e: MouseEvent) => {
      if (settingsRef.current && !settingsRef.current.contains(e.target as Node)) {
        setShowSettings(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [showSettings]);

  // Close shortcut help on outside click
  useEffect(() => {
    if (!showShortcutHelp) return;
    const handleClick = (e: MouseEvent) => {
      if (shortcutHelpRef.current && !shortcutHelpRef.current.contains(e.target as Node)) {
        setShowShortcutHelp(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [showShortcutHelp]);

  // Keyboard shortcuts
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      const tag = document.activeElement?.tagName;
      const isInput = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';

      if (e.key === 'Escape') {
        if (showNewTaskModal) { setShowNewTaskModal(false); return; }
        if (logTask) { closeLogViewer(); return; }
        if (showResearchBrowser) { setShowResearchBrowser(false); return; }
        if (showDirections) { setShowDirections(false); return; }
        if (showSettings) { setShowSettings(false); return; }
        if (showShortcutHelp) { setShowShortcutHelp(false); return; }
        return;
      }

      if (isInput) return;

      switch (e.key) {
        case 'n':
          if (!showNewTaskModal && !logTask && !showResearchBrowser && !showDirections && !showSettings) {
            openNewTaskModal();
          }
          break;
        case 'r':
          if (!showNewTaskModal && !logTask && !showResearchBrowser && !showDirections && !showSettings) {
            fetchTasks();
          }
          break;
        case '1':
          setViewMode('kanban');
          break;
        case '2':
          setViewMode('pipeline');
          break;
        case 'd':
          setShowDirections(prev => !prev);
          break;
        case 's':
          setShowSettings(prev => !prev);
          break;
        case '?':
          setShowShortcutHelp(prev => !prev);
          break;
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [showNewTaskModal, logTask, showResearchBrowser, showDirections, showSettings, showShortcutHelp]);

  const fetchTasks = async () => {
    try {
      const offset = (page - 1) * limit;
      const res = await authGet(`${API_BASE_URL}/api/admin/tasks?limit=${limit}&offset=${offset}`);
      if (res.ok) {
        const data = await res.json();
        setTasks(data || []);
        setLastRefresh(new Date());
        setError(null);
      } else {
        setError(`Failed to fetch tasks: ${res.statusText}`);
      }
    } catch (err) {
      console.error('Failed to fetch tasks:', err);
      setError('Failed to connect to backend server.');
    } finally {
      setLoading(false);
    }
  };

  const fetchProjects = async () => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/projects`);
      if (res.ok) {
        const data = await res.json();
        setProjects(data || []);
      }
    } catch (err) {
      console.error('Failed to fetch projects:', err);
    }
  };

  const handleApprove = async (taskId: string) => {
    setActionLoadingMap(m => ({ ...m, [taskId]: 'approve' }));
    try {
      await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ requires_approval: false, status: 'QUEUED' }),
      });
      fetchTasks();
    } catch (error) {
      console.error('Failed to approve task:', error);
      addToast('Failed to approve task', 'error');
    } finally {
      setActionLoadingMap(m => { const { [taskId]: _, ...rest } = m; return rest; });
    }
  };

  const handleCancel = async (taskId: string) => {
    setActionLoadingMap(m => ({ ...m, [taskId]: 'cancel' }));
    try {
      await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ status: 'CANCELLED' }),
      });
      fetchTasks();
    } catch (error) {
      console.error('Failed to cancel task:', error);
      addToast('Failed to cancel task', 'error');
    } finally {
      setActionLoadingMap(m => { const { [taskId]: _, ...rest } = m; return rest; });
    }
  };

  const handleRetry = async (taskId: string) => {
    setActionLoadingMap(m => ({ ...m, [taskId]: 'retry' }));
    try {
      await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ status: 'QUEUED' }),
      });
      fetchTasks();
    } catch (error) {
      console.error('Failed to retry task:', error);
      addToast('Failed to retry task', 'error');
    } finally {
      setActionLoadingMap(m => { const { [taskId]: _, ...rest } = m; return rest; });
    }
  };

  const handleEditPrompt = async (taskId: string, newPrompt: string) => {
    setActionLoadingMap(m => ({ ...m, [taskId]: 'edit' }));
    try {
      await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ payloads: [{ prompt: newPrompt }] }),
      });
      fetchTasks();
    } catch (error) {
      console.error('Failed to edit task:', error);
      addToast('Failed to edit task', 'error');
    } finally {
      setActionLoadingMap(m => { const { [taskId]: _, ...rest } = m; return rest; });
    }
  };

  const handleDismiss = async (taskId: string) => {
    setActionLoadingMap(m => ({ ...m, [taskId]: 'dismiss' }));
    try {
      await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ status: 'CANCELLED' }),
      });
      fetchTasks();
    } catch (error) {
      console.error('Failed to dismiss task:', error);
      addToast('Failed to dismiss task', 'error');
    } finally {
      setActionLoadingMap(m => { const { [taskId]: _, ...rest } = m; return rest; });
    }
  };

  const fetchSettings = async () => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/settings`);
      if (res.ok) {
        const data = await res.json();
        setMaxConcurrentVMs(data.max_concurrent_vms);
        setMaxRetries(data.max_retries ?? 3);
        setUsingMaxPlan(data.using_max_plan ?? false);
        setMaxPlanAccounts(data.max_plan_accounts || []);
        setGithubAccount(data.github_account || null);
      }
    } catch (err) {
      console.error('Failed to fetch settings:', err);
    }
  };

  const addMaxPlanAccount = async () => {
    if (!newAccountName || !newAccountCreds) return;
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/max-plan-accounts`, {
        method: 'POST', headers: await authHeaders(),
        body: JSON.stringify({ name: newAccountName, credentials_json: newAccountCreds }),
      });
      if (res.ok) {
        setNewAccountName('');
        setNewAccountCreds('');
        fetchSettings();
      }
    } catch (err) { console.error('Failed to add account:', err); }
  };

  const removeMaxPlanAccount = async (id: string) => {
    try {
      await fetch(`${API_BASE_URL}/api/admin/max-plan-accounts/${id}`, {
        method: 'DELETE', headers: await authHeaders(),
      });
      fetchSettings();
    } catch (err) { console.error('Failed to remove account:', err); }
  };

  const toggleMaxPlanAccount = async (id: string, enabled: boolean) => {
    try {
      await fetch(`${API_BASE_URL}/api/admin/max-plan-accounts/${id}`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ enabled }),
      });
      fetchSettings();
    } catch (err) { console.error('Failed to toggle account:', err); }
  };

  const connectGitHub = async () => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/github/oauth/authorize`);
      if (res.ok) {
        const { url } = await res.json();
        window.location.href = url;
      }
    } catch (err) { console.error('Failed to start GitHub OAuth:', err); }
  };

  const updateMaxVMs = async (value: number) => {
    const clamped = Math.max(1, Math.min(30, value));
    setMaxConcurrentVMs(clamped);
    try {
      await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ max_concurrent_vms: clamped }),
      });
    } catch (err) {
      console.error('Failed to update settings:', err);
    }
  };

  const updateMaxRetries = (value: number) => {
    const v = Math.max(0, Math.min(10, value));
    setMaxRetries(v);
    authHeaders().then(h => fetch(`${API_BASE_URL}/api/admin/settings`, {
      method: 'PATCH', headers: h,
      body: JSON.stringify({ max_retries: v }),
    })).catch(() => {});
  };

  const handleToggleSettings = () => {
    setShowSettings(prev => {
      if (!prev) fetchSettings();
      return !prev;
    });
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
    setUseManualRepo(false);
    setUseManualBranch(false);
    setBranchOptions([]);
    setBranchError(null);
    fetchRepoOptions();
  };

  const handleRepoChange = (value: string) => {
    setNewTaskRepo(value);
    const repoMeta = repoOptions.find(r => r.full_name === value);
    if (repoMeta) {
      setNewTaskBranch(repoMeta.default_branch || 'main');
    } else {
      setNewTaskBranch('main');
    }
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
      fetchTasks();
    } catch (error) {
      console.error('Failed to create task:', error);
    }
  };

  const handleViewLogs = useCallback((task: Task) => {
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setLogTask(task);
    setLogLines(task.error_logs ? [`[Error Log]\n${task.error_logs}`] : []);

    if (task.status !== 'IN_PROGRESS' && task.status !== 'PROVISIONING') return;
    if (!task.project_id) return;

    (async () => {
      try {
        const ticketRes = await fetch(`${API_BASE_URL}/api/projects/${task.project_id}/ws-ticket`, {
          method: 'POST',
          headers: await authHeaders(),
        });
        if (!ticketRes.ok) {
          setLogLines(prev => [...prev, '[Failed to get WebSocket ticket]']);
          return;
        }
        const { ticket } = await ticketRes.json();

        const wsUrl = (API_BASE_URL || window.location.origin).replace(/^http/, 'ws');
        const ws = new WebSocket(`${wsUrl}/api/projects/${task.project_id}/ws`);
        wsRef.current = ws;

        ws.onopen = () => {
          ws.send(JSON.stringify({ ticket }));
          setLogLines(prev => [...prev, '[Connected to live log stream]']);
        };

        ws.onmessage = (event) => {
          try {
            const msg = JSON.parse(event.data);
            if (msg.event === 'agent_log' && msg.data?.task_id === task.id) {
              setLogLines(prev => [...prev, msg.data.log]);
            } else if (msg.event === 'task_updated' && msg.data?.task_id === task.id) {
              setLogLines(prev => [...prev, `[Status: ${msg.data.status}]`]);
            }
            if (msg.event === 'task_updated' || msg.event === 'artifact_created') {
              if (fetchTasksDebounceRef.current) clearTimeout(fetchTasksDebounceRef.current);
              fetchTasksDebounceRef.current = setTimeout(() => { fetchTasks(); }, 500);
            }
          } catch {
            // ignore parse errors
          }
        };

        ws.onerror = (e) => {
          console.error('[WS] error:', e);
          setLogLines(prev => [...prev, `[WebSocket error — check browser console (F12)]`]);
        };

        ws.onclose = (e) => {
          console.log('[WS] closed:', e.code, e.reason);
          setLogLines(prev => [...prev, `[Stream disconnected: code=${e.code} reason=${e.reason || 'none'}]`]);
        };
      } catch (err) {
        setLogLines(prev => [...prev, `[Connection error: ${err}]`]);
      }
    })();
  }, []);

  const closeLogViewer = () => {
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setLogTask(null);
    setLogLines([]);
  };

  return {
    // Auth
    user, authLoading, error, setError,
    // Tasks
    tasks, loading, page, setPage, limit,
    actionLoadingMap,
    fetchTasks, handleApprove, handleCancel, handleRetry, handleEditPrompt, handleDismiss,
    // Toasts
    toasts, dismissToast,
    // View
    viewMode, setViewMode,
    // Search/filter
    searchQuery, setSearchQuery, taskTypeFilter, setTaskTypeFilter,
    // Projects
    projects, selectedProjectId, setSelectedProjectId,
    // Log viewer
    logTask, logLines, handleViewLogs, closeLogViewer,
    // Research browser
    showResearchBrowser, setShowResearchBrowser,
    // New task modal
    showNewTaskModal, setShowNewTaskModal,
    newTaskRepo, newTaskBranch, newTaskTitle, newTaskPrompt, newTaskType,
    newTaskPrNumber, newTaskPrBranch,
    setNewTaskRepo, setNewTaskBranch, setNewTaskTitle, setNewTaskPrompt,
    setNewTaskType, setNewTaskPrNumber, setNewTaskPrBranch,
    repoOptions, repoLoading, repoError, useManualRepo, setUseManualRepo,
    branchOptions, branchLoading, branchError, useManualBranch, setUseManualBranch, setBranchOptions,
    openNewTaskModal, handleRepoChange, handleCreateTask,
    // Settings
    showSettings, settingsRef, handleToggleSettings,
    maxConcurrentVMs, maxRetries, usingMaxPlan,
    updateMaxVMs, updateMaxRetries,
    // Max plan accounts
    maxPlanAccounts, newAccountName, setNewAccountName, newAccountCreds, setNewAccountCreds,
    addMaxPlanAccount, removeMaxPlanAccount, toggleMaxPlanAccount,
    // GitHub
    githubAccount, connectGitHub,
    // Directions
    showDirections, setShowDirections,
    // Misc
    lastRefresh, wsRef,
    showShortcutHelp, setShowShortcutHelp, shortcutHelpRef,
  };
}
