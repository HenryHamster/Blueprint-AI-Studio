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
  const [totalTasks, setTotalTasks] = useState(0);
  const [globalActiveCount, setGlobalActiveCount] = useState(0);
  const [globalQueuedCount, setGlobalQueuedCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [limit, setLimit] = usePersistedState<number>('pageSize', 50);
  const [refreshInterval, setRefreshInterval] = usePersistedState<number>('refreshInterval', 30000);

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
  const usingMaxPlanRef = useRef(usingMaxPlan);
  useEffect(() => { usingMaxPlanRef.current = usingMaxPlan; }, [usingMaxPlan]);

  // Max plan pool state
  const [maxPlanAccounts, setMaxPlanAccounts] = useState<MaxPlanAccount[]>([]);
  const [newAccountName, setNewAccountName] = useState('');
  const [newAccountToken, setNewAccountToken] = useState('');

  // GitHub connection state
  const [githubAccount, setGithubAccount] = useState<string | null>(null);

  // Defaults
  const [defaultTaskType, setDefaultTaskType] = usePersistedState<'create' | 'plan' | 'research' | 'review' | 'fix'>('defaultTaskType', 'create');
  const [notifyOnComplete, setNotifyOnComplete] = usePersistedState<boolean>('notifyOnComplete', false);
  const prevTasksRef = useRef<Task[]>([]);
  const notifyRef = useRef(notifyOnComplete);
  useEffect(() => { notifyRef.current = notifyOnComplete; }, [notifyOnComplete]);

  // View toggle state
  const [viewMode, setViewMode] = usePersistedState<'kanban' | 'pipeline'>('viewMode', 'kanban');

  // Project dashboard state
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProjectId, setSelectedProjectId] = usePersistedState<string | null>('selectedProject', null);

  // Directions panel state
  const [showDirections, setShowDirections] = useState(false);

  // Metrics dashboard state
  const [showMetrics, setShowMetrics] = useState(false);
  const [metricsHistory, setMetricsHistory] = useState<{ timestamp: number; activeVMs: number; costPerHr: number; completedCount: number; failedCount: number }[]>([]);
  const [cumulativeCost, setCumulativeCost] = useState(0);
  const metricsHistoryRef = useRef<{ timestamp: number; activeVMs: number; costPerHr: number; completedCount: number; failedCount: number }[]>([]);

  // Refresh indicator state
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

  // Status bar state
  const [apiHealthy, setApiHealthy] = useState(true);
  const [wsConnected, setWsConnected] = useState(false);
  const [lastFetchTime, setLastFetchTime] = useState<number>(0);

  // Search and filter state
  const [searchQuery, setSearchQuery] = useState('');
  const [taskTypeFilter, setTaskTypeFilter] = useState('all');
  const [statusFilter, setStatusFilter] = useState<string | null>(null);

  // Reset page when status filter changes
  useEffect(() => { setPage(1); }, [statusFilter]);

  // Keyboard shortcut help state
  const [showShortcutHelp, setShowShortcutHelp] = useState(false);
  const shortcutHelpRef = useRef<HTMLDivElement>(null);

  // Archive view state
  const [showArchive, setShowArchiveState] = useState(false);
  const [archiveTasks, setArchiveTasks] = useState<Task[]>([]);
  const [archiveLoading, setArchiveLoading] = useState(false);
  const [archivePage, setArchivePageState] = useState(1);
  const [archivePageSize, setArchivePageSizeState] = usePersistedState<number>('archivePageSize', 50);
  const [archiveStatusFilter, setArchiveStatusFilterState] = useState<string>('all');
  const [archiveSearchQuery, setArchiveSearchQueryState] = useState('');
  const [archiveDateRange, setArchiveDateRangeState] = useState<string>('all');
  const [archiveTotalTasks, setArchiveTotalTasks] = useState(0);
  const [archiveSortOrder, setArchiveSortOrderState] = useState<'desc' | 'asc'>('desc');

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
    const projectInterval = setInterval(fetchProjects, 60000);
    if (refreshInterval === 0) {
      return () => { clearInterval(projectInterval); };
    }
    const interval = setInterval(fetchTasks, refreshInterval);
    return () => { clearInterval(interval); clearInterval(projectInterval); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, limit, user, statusFilter, refreshInterval]);

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
      const targetTag = (e.target as HTMLElement)?.tagName;
      const isInput = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT'
        || targetTag === 'INPUT' || targetTag === 'TEXTAREA' || targetTag === 'SELECT';

      if (e.key === 'Escape') {
        if (showNewTaskModal) { setShowNewTaskModal(false); return; }
        if (logTask) { closeLogViewer(); return; }
        if (showResearchBrowser) { setShowResearchBrowser(false); return; }
        if (showArchive) { setShowArchiveState(false); return; }
        if (showDirections) { setShowDirections(false); return; }
        if (showMetrics) { setShowMetrics(false); return; }
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
        case 'm':
          setShowMetrics(prev => !prev);
          break;
        case '?':
          setShowShortcutHelp(prev => !prev);
          break;
        case 'a':
        case 'A':
          if (!showNewTaskModal && !logTask && !showResearchBrowser && !showDirections && !showSettings && !showShortcutHelp) {
            setShowArchive(!showArchive);
          }
          break;
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showNewTaskModal, logTask, showResearchBrowser, showDirections, showMetrics, showSettings, showShortcutHelp, showArchive]);

  const fetchTasks = async () => {
    try {
      const offset = (page - 1) * limit;
      let url = `${API_BASE_URL}/api/admin/tasks?limit=${limit}&offset=${offset}`;
      if (statusFilter) url += `&status=${statusFilter}`;
      const res = await authGet(url);
      if (res.ok) {
        const data = await res.json();
        let newTasks: Task[];
        if (Array.isArray(data)) {
          newTasks = data;
          setTotalTasks(data.length);
        } else {
          newTasks = data.tasks || [];
          setTotalTasks(data.total ?? newTasks.length);
          if (data.active_count !== undefined) setGlobalActiveCount(data.active_count);
          if (data.queued_count !== undefined) setGlobalQueuedCount(data.queued_count);
        }

        // Browser notifications for completed/failed tasks
        if (notifyRef.current && prevTasksRef.current.length > 0) {
          const prevMap = new Map(prevTasksRef.current.map(t => [t.id, t.status]));
          for (const t of newTasks) {
            const prev = prevMap.get(t.id);
            if (prev && prev !== t.status && (t.status === 'COMPLETED' || t.status === 'FAILED')) {
              if (Notification.permission === 'granted') {
                new Notification(`Task ${t.status.toLowerCase()}`, {
                  body: t.title || t.id,
                });
              }
            }
          }
        }
        // Count incremental completed/failed transitions since last snapshot
        let completedCount = 0;
        let failedCount = 0;
        if (prevTasksRef.current.length > 0) {
          const prevMap = new Map(prevTasksRef.current.map(t => [t.id, t.status]));
          for (const t of newTasks) {
            const prev = prevMap.get(t.id);
            if (prev && prev !== t.status) {
              if (t.status === 'COMPLETED') completedCount++;
              else if (t.status === 'FAILED') failedCount++;
            }
          }
        }

        prevTasksRef.current = newTasks;

        setTasks(newTasks);
        setLastRefresh(new Date());
        setError(null);
        setApiHealthy(true);
        setLastFetchTime(Date.now());

        // Always record metrics data points (rendering depends on showMetrics, not collection)
        const now = Date.now();
        const activeVMs = data.active_count ?? newTasks.filter(t => t.status === 'IN_PROGRESS' || t.status === 'PROVISIONING' || t.status === 'EVALUATING').length;
        const costPerHr = activeVMs * (usingMaxPlanRef.current ? 0.07 : 1.07);

        // Compute cumulative cost using the rate from the *previous* interval
        const prevHistory = metricsHistoryRef.current;
        const lastPoint = prevHistory[prevHistory.length - 1];
        if (lastPoint) {
          const intervalMs = now - lastPoint.timestamp;
          setCumulativeCost(c => c + lastPoint.costPerHr * (intervalMs / 3600000));
        }

        const filtered = prevHistory.filter(p => now - p.timestamp <= 7200000);
        const newHistory = [...filtered, { timestamp: now, activeVMs, costPerHr, completedCount, failedCount }];
        metricsHistoryRef.current = newHistory;
        setMetricsHistory(newHistory);
      } else {
        setError(`Failed to fetch tasks: ${res.statusText}`);
        setApiHealthy(false);
      }
    } catch (err) {
      console.error('Failed to fetch tasks:', err);
      setError('Failed to connect to backend server.');
      setApiHealthy(false);
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
        body: JSON.stringify({ requires_approval: false }),
      });
      fetchTasks();
    } catch (error) {
      console.error('Failed to approve task:', error);
      addToast('Failed to approve task', 'error');
    } finally {
      setActionLoadingMap(m => { const { [taskId]: _unused, ...rest } = m; void _unused; return rest; });
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
      setActionLoadingMap(m => { const { [taskId]: _unused, ...rest } = m; void _unused; return rest; });
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
      fetchArchiveTasks();
    } catch (error) {
      console.error('Failed to retry task:', error);
      addToast('Failed to retry task', 'error');
    } finally {
      setActionLoadingMap(m => { const { [taskId]: _unused, ...rest } = m; void _unused; return rest; });
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
      setActionLoadingMap(m => { const { [taskId]: _unused, ...rest } = m; void _unused; return rest; });
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
      setActionLoadingMap(m => { const { [taskId]: _unused, ...rest } = m; void _unused; return rest; });
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
    if (!newAccountName || !newAccountToken) return;
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/max-plan-accounts`, {
        method: 'POST', headers: await authHeaders(),
        body: JSON.stringify({ name: newAccountName, oauth_token: newAccountToken }),
      });
      if (res.ok) {
        setNewAccountName('');
        setNewAccountToken('');
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
    setNewTaskType(defaultTaskType);
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
          setWsConnected(true);
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
          setWsConnected(false);
        };
      } catch (err) {
        setLogLines(prev => [...prev, `[Connection error: ${err}]`]);
      }
    })();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const closeLogViewer = () => {
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setWsConnected(false);
    setLogTask(null);
    setLogLines([]);
  };

  const fetchArchiveTasks = async (
    pageOverride?: number,
    pageSizeOverride?: number,
    statusOverride?: string,
    searchOverride?: string,
    dateRangeOverride?: string,
    sortOrderOverride?: 'desc' | 'asc',
  ) => {
    setArchiveLoading(true);
    try {
      const p = pageOverride ?? archivePage;
      const ps = pageSizeOverride ?? archivePageSize;
      const status = statusOverride ?? archiveStatusFilter;
      const search = searchOverride ?? archiveSearchQuery;
      const dateRange = dateRangeOverride ?? archiveDateRange;
      const sortOrder = sortOrderOverride ?? archiveSortOrder;
      const offset = (p - 1) * ps;
      let url = `${API_BASE_URL}/api/admin/tasks?limit=${ps}&offset=${offset}`;
      if (status !== 'all') {
        url += `&status=${status}`;
      } else {
        url += `&terminal_only=true`;
      }
      if (search.trim()) {
        url += `&search=${encodeURIComponent(search.trim())}`;
      }
      if (dateRange !== 'all') {
        const now = new Date();
        const hours = dateRange === '24h' ? 24 : dateRange === '7d' ? 24 * 7 : dateRange === '30d' ? 24 * 30 : 0;
        if (hours > 0) {
          const after = new Date(now.getTime() - hours * 60 * 60 * 1000);
          url += `&created_after=${encodeURIComponent(after.toISOString())}`;
        }
      }
      url += `&sort_order=${sortOrder}`;
      const res = await authGet(url);
      if (res.ok) {
        const data = await res.json();
        let tasks: Task[] = data.tasks || [];
        // Frontend fallback sort in case the backend ignores the sort_order param
        tasks = [...tasks].sort((a, b) => {
          const diff = new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
          return sortOrder === 'asc' ? -diff : diff;
        });
        setArchiveTasks(tasks);
        setArchiveTotalTasks(data.total || 0);
      }
    } catch (err) {
      console.error('Failed to fetch archive tasks:', err);
    } finally {
      setArchiveLoading(false);
    }
  };

  const setShowArchive = (show: boolean) => {
    setShowArchiveState(show);
    if (show) {
      fetchArchiveTasks();
    }
  };

  const setArchivePage = (page: number) => {
    setArchivePageState(page);
    fetchArchiveTasks(page);
  };

  const setArchivePageSize = (size: number) => {
    setArchivePageSizeState(size);
    setArchivePageState(1);
    fetchArchiveTasks(1, size);
  };

  const setArchiveStatusFilter = (status: string) => {
    setArchiveStatusFilterState(status);
    setArchivePageState(1);
    fetchArchiveTasks(1, undefined, status);
  };

  const archiveSearchDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const setArchiveSearchQuery = (query: string) => {
    setArchiveSearchQueryState(query);
    if (archiveSearchDebounceRef.current) {
      clearTimeout(archiveSearchDebounceRef.current);
    }
    archiveSearchDebounceRef.current = setTimeout(() => {
      setArchivePageState(1);
      fetchArchiveTasks(1, undefined, undefined, query);
    }, 300);
  };

  const setArchiveDateRange = (range: string) => {
    setArchiveDateRangeState(range);
    setArchivePageState(1);
    fetchArchiveTasks(1, undefined, undefined, undefined, range);
  };

  const setArchiveSortOrder = (order: 'desc' | 'asc') => {
    setArchiveSortOrderState(order);
    setArchivePageState(1);
    fetchArchiveTasks(1, undefined, undefined, undefined, undefined, order);
  };

  return {
    // Auth
    user, authLoading, error, setError,
    // Tasks
    tasks, totalTasks, globalActiveCount, globalQueuedCount, loading, page, setPage, limit, setLimit, refreshInterval, setRefreshInterval,
    actionLoadingMap,
    fetchTasks, handleApprove, handleCancel, handleRetry, handleEditPrompt, handleDismiss,
    // Toasts
    toasts, dismissToast,
    // View
    viewMode, setViewMode,
    // Search/filter
    searchQuery, setSearchQuery, taskTypeFilter, setTaskTypeFilter, statusFilter, setStatusFilter,
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
    // Defaults
    defaultTaskType, setDefaultTaskType,
    notifyOnComplete, setNotifyOnComplete,
    // Settings
    showSettings, settingsRef, handleToggleSettings,
    maxConcurrentVMs, maxRetries, usingMaxPlan,
    updateMaxVMs, updateMaxRetries,
    // Max plan accounts
    maxPlanAccounts, newAccountName, setNewAccountName, newAccountToken, setNewAccountToken,
    addMaxPlanAccount, removeMaxPlanAccount, toggleMaxPlanAccount,
    // GitHub
    githubAccount, connectGitHub,
    // Directions
    showDirections, setShowDirections,
    // Metrics
    showMetrics, setShowMetrics, metricsHistory, cumulativeCost,
    // Archive
    showArchive, setShowArchive, archiveTasks, archiveLoading,
    archivePage, setArchivePage, archivePageSize, setArchivePageSize,
    archiveStatusFilter, setArchiveStatusFilter,
    archiveSearchQuery, setArchiveSearchQuery,
    archiveDateRange, setArchiveDateRange,
    archiveTotalTasks, fetchArchiveTasks,
    archiveSortOrder, setArchiveSortOrder,
    // Misc
    lastRefresh, wsRef,
    showShortcutHelp, setShowShortcutHelp, shortcutHelpRef,
    // Status bar
    apiHealthy, wsConnected, lastFetchTime,
  };
}
