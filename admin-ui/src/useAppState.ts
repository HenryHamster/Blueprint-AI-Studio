import { useState, useEffect, useRef, useCallback } from 'react';
import type { Task } from './components/TaskCard';
import type { Project } from './components/ProjectDashboard';
import type { MaxPlanAccount } from './components/SettingsDropdown';
import type { Alert } from './components/AlertsBanner';
import { onAuthChange } from './firebase';
import type { User } from 'firebase/auth';
import { API_BASE_URL, authHeaders, authGet, COST_PER_VM_HR, fetchTask } from './api';
import { useToasts } from './components/Toasts';
import { STATUS_OPTIONS } from './constants';

export interface DeployState {
  status: string;
  current_revision: string;
  current_sha: string;
  deployed_at: string;
  last_known_good_revision: string;
  last_known_good_sha: string;
  rollback_reason: string | null;
  rollback_count: number;
  pause_reason: string | null;
  failure_rate: number;
  monitoring_window: { elapsed_seconds: number; total_seconds: number; percent: number } | null;
  task_results_count: number;
  gcp_project_id: string;
}

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

function playCompletionSound() {
  try {
    const ctx = new AudioContext();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.frequency.setValueAtTime(880, ctx.currentTime);
    osc.frequency.setValueAtTime(1100, ctx.currentTime + 0.1);
    gain.gain.setValueAtTime(0.3, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.4);
    osc.start(ctx.currentTime);
    osc.stop(ctx.currentTime + 0.4);
  } catch { /* AudioContext not available */ }
}

export function useAppState() {
  const [user, setUser] = useState<User | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [totalTasks, setTotalTasks] = useState(0);
  const [globalActiveCount, setGlobalActiveCount] = useState(0);
  const [globalEvaluatingCount, setGlobalEvaluatingCount] = useState(0);
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
  const [newTaskType, setNewTaskType] = useState<'create' | 'plan' | 'research' | 'review' | 'fix' | 'migrate' | 'refactor'>('create');
  const [newTaskPrNumber, setNewTaskPrNumber] = useState('');
  const [newTaskPrBranch, setNewTaskPrBranch] = useState('');
  const [newTaskModel, setNewTaskModel] = useState('');
  const [newTaskEffort, setNewTaskEffort] = useState('');

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
  const [alerts, setAlerts] = useState<Alert[]>([]);

  // Log viewer state
  const [logTask, setLogTask] = useState<Task | null>(null);
  const [logLines, setLogLines] = useState<string[]>([]);
  const [logsConnecting, setLogsConnecting] = useState(false);
  const [wsDisconnected, setWsDisconnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  // Research browser state
  const [showResearchBrowser, setShowResearchBrowser] = useState(false);

  // Activity feed state
  const [showActivityFeed, setShowActivityFeed] = useState(false);

  // Settings state
  const [showSettings, setShowSettings] = useState(false);
  const [maxConcurrentVMs, setMaxConcurrentVMs] = useState(5);
  const [maxRetries, setMaxRetries] = useState(3);
  const [maxAutonomousTasksPerHour, setMaxAutonomousTasksPerHour] = useState(10);
  const [autonomousResearchIntervalMinutes, setAutonomousResearchIntervalMinutes] = useState(30);
  const [autonomousResearchModel, setAutonomousResearchModel] = useState('');
  const [autonomousResearchEffort, setAutonomousResearchEffort] = useState('');
  const [usingMaxPlan, setUsingMaxPlan] = useState(false);
  const settingsRef = useRef<HTMLDivElement>(null);
  const fetchTasksDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fetchTasksRef = useRef<() => Promise<void>>(async () => {});
  // Max plan pool state
  const [maxPlanAccounts, setMaxPlanAccounts] = useState<MaxPlanAccount[]>([]);
  const [newAccountName, setNewAccountName] = useState('');
  const [newAccountToken, setNewAccountToken] = useState('');

  // GitHub connection state
  const [githubAccount, setGithubAccount] = useState<string | null>(null);

  // Defaults
  const [defaultTaskType, setDefaultTaskType] = usePersistedState<'create' | 'plan' | 'research' | 'review' | 'fix' | 'migrate' | 'refactor'>('defaultTaskType', 'create');
  const [notifyOnComplete, setNotifyOnComplete] = usePersistedState<boolean>('notifyOnComplete', false);
  const [soundOnComplete, setSoundOnComplete] = usePersistedState<boolean>('soundOnComplete', false);
  const prevTasksRef = useRef<Task[]>([]);
  const notifyRef = useRef(notifyOnComplete);
  useEffect(() => { notifyRef.current = notifyOnComplete; }, [notifyOnComplete]);
  const soundOnCompleteRef = useRef(soundOnComplete);
  useEffect(() => { soundOnCompleteRef.current = soundOnComplete; }, [soundOnComplete]);

  // View toggle state
  const [viewMode, setViewMode] = usePersistedState<'kanban' | 'pipeline'>('viewMode', 'kanban');
  const [compactMode, setCompactMode] = usePersistedState<boolean>('compactMode', false);
  const [groupBy, setGroupBy] = usePersistedState<'status' | 'type' | 'project'>('groupBy', 'status');

  // Kanban column preferences (order + visibility)
  const [columnPrefs, setColumnPrefs] = usePersistedState<{ id: string; visible: boolean }[]>('kanban_column_prefs', []);

  // Project dashboard state
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectsFetchError, setProjectsFetchError] = useState<string | null>(null);
  const [selectedProjectId, setSelectedProjectId] = usePersistedState<string | null>('selectedProject', null);

  // Directions panel state
  const [showDirections, setShowDirections] = useState(false);

  // Metrics dashboard state
  const [showMetrics, setShowMetrics] = useState(false);
  const [metricsHistory, setMetricsHistory] = usePersistedState<{ timestamp: number; activeVMs: number; costPerHr: number; completedCount: number; failedCount: number }[]>('metricsHistory', []);
  const [cumulativeCost, setCumulativeCost] = usePersistedState<number>('cumulativeCost', 0);
  const [cumulativeVmHours, setCumulativeVmHours] = usePersistedState<number>('cumulativeVmHours', 0);
  const [metricsWindow, setMetricsWindow] = usePersistedState<'15m' | '30m' | '1h' | '2h' | '3h' | '6h' | '12h' | '24h' | 'all'>('metricsWindow', '1h');
  const metricsHistoryRef = useRef<{ timestamp: number; activeVMs: number; costPerHr: number; completedCount: number; failedCount: number }[]>([]);
  useEffect(() => { metricsHistoryRef.current = metricsHistory.filter(p => Date.now() - p.timestamp <= 86400000); }, [metricsHistory]);

  // Refresh indicator state
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

  // Status bar state
  const [apiHealthy, setApiHealthy] = useState(true);
  const [wsConnected, setWsConnected] = useState(false);
  const [lastFetchTime, setLastFetchTime] = useState<number>(0);

  // Search and filter state
  const [searchQuery, setSearchQuery] = useState('');
  const [taskTypeFilter, setTaskTypeFilter] = useState<string[]>(['create', 'plan', 'research', 'review', 'fix', 'migrate', 'refactor']);
  const [statusFilter, setStatusFilter] = useState<string[]>(['QUEUED', 'PROVISIONING', 'IN_PROGRESS', 'EVALUATING', 'FAILED', 'CANCELLED', 'COMPLETED']);

  // Reset page when status filter changes
  useEffect(() => { setPage(1); }, [statusFilter]);

  // Keyboard shortcut help state
  const [showShortcutHelp, setShowShortcutHelp] = useState(false);
  const shortcutHelpRef = useRef<HTMLDivElement>(null);

  // Selection mode state
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedTaskIds, setSelectedTaskIds] = useState<Set<string>>(new Set());

  // Confirm dialog state
  const [confirmDialog, setConfirmDialog] = useState<{ title: string; message: string; confirmLabel: string; onConfirm: () => void } | null>(null);

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

  // Immediate fetch when filters, page, or project selection change
  useEffect(() => {
    if (!user) return;
    fetchTasks();
    fetchAlerts();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, limit, user, statusFilter, selectedProjectId]);

  // Polling interval — only restarts when refreshInterval or user changes
  useEffect(() => {
    if (!user) return;
    fetchSettings();
    fetchProjects();
    fetchAlerts();
    const projectInterval = setInterval(fetchProjects, 60000);
    if (refreshInterval === 0) {
      return () => { clearInterval(projectInterval); };
    }
    const interval = setInterval(() => { fetchTasksRef.current(); fetchAlerts(); }, refreshInterval);
    return () => { clearInterval(interval); clearInterval(projectInterval); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshInterval, user]);

  // Clear wsDisconnected banner when task reaches a terminal state
  useEffect(() => {
    if (logTask && ['COMPLETED', 'FAILED', 'CANCELLED'].includes(logTask.status)) {
      setWsDisconnected(false);
    }
  }, [logTask, logTask?.status]);

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
        if (showActivityFeed) { setShowActivityFeed(false); return; }
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
  }, [showNewTaskModal, logTask, showResearchBrowser, showActivityFeed, showDirections, showMetrics, showSettings, showShortcutHelp, showArchive]);

  const fetchTasks = async () => {
    try {
      const offset = (page - 1) * limit;
      let url = `${API_BASE_URL}/api/admin/tasks?limit=${limit}&offset=${offset}&archived=false`;
      if (statusFilter.length < STATUS_OPTIONS.length) {
        statusFilter.forEach(s => { url += `&status=${s}`; });
      }
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
          if (data.evaluating_count !== undefined) setGlobalEvaluatingCount(data.evaluating_count);
          if (data.queued_count !== undefined) setGlobalQueuedCount(data.queued_count);
        }

        // Browser notifications for completed/failed tasks
        if (prevTasksRef.current.length > 0) {
          const prevMap = new Map(prevTasksRef.current.map(t => [t.id, t.status]));
          let didTransition = false;
          for (const t of newTasks) {
            const prev = prevMap.get(t.id);
            if (prev && prev !== t.status && (t.status === 'COMPLETED' || t.status === 'FAILED')) {
              if (notifyRef.current && Notification.permission === 'granted') {
                new Notification(`Task ${t.status.toLowerCase()}`, {
                  body: t.title || t.id,
                });
              }
              didTransition = true;
            }
          }
          if (didTransition && soundOnCompleteRef.current) {
            playCompletionSound();
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
        const activeVMs = data.active_count ?? newTasks.filter(t => t.status === 'IN_PROGRESS' || t.status === 'PROVISIONING').length;
        const costPerHr = activeVMs * COST_PER_VM_HR;

        // Compute cumulative cost and VM-hours using the rate from the *previous* interval
        const prevHistory = metricsHistoryRef.current;
        const lastPoint = prevHistory[prevHistory.length - 1];
        if (lastPoint) {
          const intervalMs = now - lastPoint.timestamp;
          const intervalHrs = intervalMs / 3600000;
          setCumulativeCost(c => c + lastPoint.costPerHr * intervalHrs);
          setCumulativeVmHours(h => h + lastPoint.activeVMs * intervalHrs);
        }

        const filtered = prevHistory.filter((p: { timestamp: number }) => now - p.timestamp <= 86400000);
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
  fetchTasksRef.current = fetchTasks;

  const fetchProjects = async () => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/projects`);
      if (res.ok) {
        const data = await res.json();
        setProjects(data || []);
        setProjectsFetchError(null);
      } else {
        setProjectsFetchError('Failed to load projects');
      }
    } catch (err) {
      console.error('Failed to fetch projects:', err);
      setProjectsFetchError('Failed to load projects');
    }
  };

  const fetchAlerts = async () => {
    try {
      const url = selectedProjectId
        ? `${API_BASE_URL}/api/admin/alerts?project_id=${selectedProjectId}`
        : `${API_BASE_URL}/api/admin/alerts`;
      const res = await authGet(url);
      if (res.ok) {
        setAlerts(await res.json());
      }
    } catch (err) {
      console.error('Failed to fetch alerts:', err);
    }
  };

  const dismissAlert = async (alertId: string) => {
    const prev = alerts;
    setAlerts(prevAlerts => prevAlerts.filter(a => a.id !== alertId));
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/alerts/${alertId}/dismiss`, {
        method: 'POST',
        headers: await authHeaders(),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (err) {
      console.error('Failed to dismiss alert:', err);
      setAlerts(prev);
      addToast('Failed to dismiss alert', 'error');
    }
  };

  const handlePauseAllAutonomous = async () => {
    const res = await fetch(`${API_BASE_URL}/api/admin/autonomous/pause-all`, {
      method: 'POST',
      headers: await authHeaders(),
    });
    if (res.ok) {
      await fetchProjects();
      addToast('All autonomous projects paused', 'success');
    } else {
      addToast('Failed to pause autonomous projects', 'error');
    }
  };

  const handleClearWaveDeps = async (projectId: string) => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${projectId}/clear-wave-deps`, {
        method: 'POST',
        headers: await authHeaders(),
        body: '{}',
      });
      const data = await res.json();
      addToast(`Cleared dependencies on ${data.cleared} tasks`, 'success');
      fetchTasks();
    } catch (error) {
      console.error('Failed to clear wave deps:', error);
      addToast('Failed to clear wave dependencies', 'error');
    }
  };

  const handleApprove = async (taskId: string) => {
    const original = tasks.find(t => t.id === taskId);
    setTasks(prev => prev.map(t => t.id === taskId ? { ...t, requires_approval: false, status: 'IN_PROGRESS' } : t));
    setActionLoadingMap(m => ({ ...m, [taskId]: 'approve' }));
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ requires_approval: false }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setTasks(prev => prev.map(t => t.id === taskId ? original! : t));
        addToast(data.detail ? `Failed to approve task: ${data.detail}` : 'Failed to approve task', 'error');
        return;
      }
      fetchTasks();
      addToast('Task approved', 'success');
    } catch (error) {
      console.error('Failed to approve task:', error);
      setTasks(prev => prev.map(t => t.id === taskId ? original! : t));
      addToast('Failed to approve task', 'error');
    } finally {
      setActionLoadingMap(m => { const { [taskId]: _unused, ...rest } = m; void _unused; return rest; });
    }
  };

  const handleRequestChanges = async (taskId: string, feedback: string, action: 'fix' | 'review' = 'fix') => {
    setActionLoadingMap(m => ({ ...m, [taskId]: 'request-changes' }));
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}/request-changes`, {
        method: 'POST',
        headers: await authHeaders(),
        body: JSON.stringify({ feedback, action }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        addToast(data.detail || 'Failed to request changes', 'error');
        return;
      }
      fetchTasks();
      addToast(action === 'fix' ? 'Fix task spawned' : 'Re-review spawned', 'success');
    } catch {
      addToast('Failed to request changes', 'error');
    } finally {
      setActionLoadingMap(m => { const { [taskId]: _unused, ...rest } = m; void _unused; return rest; });
    }
  };

  const handleCancel = async (taskId: string) => {
    const original = tasks.find(t => t.id === taskId);
    setTasks(prev => prev.map(t => t.id === taskId ? { ...t, status: 'CANCELLED' } : t));
    setActionLoadingMap(m => ({ ...m, [taskId]: 'cancel' }));
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ status: 'CANCELLED' }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setTasks(prev => prev.map(t => t.id === taskId ? original! : t));
        addToast(data.detail ? `Failed to cancel task: ${data.detail}` : 'Failed to cancel task', 'error');
        return;
      }
      fetchTasks();
      addToast('Task cancelled', 'success');
    } catch (error) {
      console.error('Failed to cancel task:', error);
      setTasks(prev => prev.map(t => t.id === taskId ? original! : t));
      addToast('Failed to cancel task', 'error');
    } finally {
      setActionLoadingMap(m => { const { [taskId]: _unused, ...rest } = m; void _unused; return rest; });
    }
  };

  const handleRetry = async (taskId: string) => {
    const original = tasks.find(t => t.id === taskId);
    setTasks(prev => prev.map(t => t.id === taskId ? { ...t, status: 'QUEUED' } : t));
    setActionLoadingMap(m => ({ ...m, [taskId]: 'retry' }));
    try {
      await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ status: 'QUEUED' }),
      });
      fetchTasks();
      fetchArchiveTasks();
      addToast('Task queued for retry', 'success');
    } catch (error) {
      console.error('Failed to retry task:', error);
      setTasks(prev => prev.map(t => t.id === taskId ? original! : t));
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
      addToast('Prompt updated', 'success');
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
      addToast('Task dismissed', 'success');
    } catch (error) {
      console.error('Failed to dismiss task:', error);
      addToast('Failed to dismiss task', 'error');
    } finally {
      setActionLoadingMap(m => { const { [taskId]: _unused, ...rest } = m; void _unused; return rest; });
    }
  };

  const handleArchiveTask = async (taskId: string) => {
    setActionLoadingMap(m => ({ ...m, [taskId]: 'archive' }));
    try {
      await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}/archive`, {
        method: 'POST',
        headers: await authHeaders(),
      });
      fetchTasks();
      addToast('Task archived', 'success');
    } catch (error) {
      console.error('Failed to archive task:', error);
      addToast('Failed to archive task', 'error');
    } finally {
      setActionLoadingMap(m => { const { [taskId]: _unused, ...rest } = m; void _unused; return rest; });
    }
  };

  const handleUnarchiveTask = async (taskId: string) => {
    setActionLoadingMap(m => ({ ...m, [taskId]: 'unarchive' }));
    try {
      await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}/unarchive`, {
        method: 'POST',
        headers: await authHeaders(),
      });
      fetchArchiveTasks();
      addToast('Task unarchived', 'success');
    } catch (error) {
      console.error('Failed to unarchive task:', error);
      addToast('Failed to unarchive task', 'error');
    } finally {
      setActionLoadingMap(m => { const { [taskId]: _unused, ...rest } = m; void _unused; return rest; });
    }
  };

  const handleRestartOrchestrator = async () => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/restart-orchestrator`, {
        method: 'POST',
        headers: await authHeaders(),
      });
      if (!res.ok) throw new Error(res.statusText);
      addToast('Orchestrator restarted', 'success');
    } catch (error) {
      console.error('Failed to restart orchestrator:', error);
      addToast('Failed to restart orchestrator', 'error');
      throw error;
    }
  };

  // --- Deploy State ---
  const [deployState, setDeployState] = useState<DeployState | null>(null);

  const fetchDeployState = async () => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/deploy-state`);
      if (res.ok) setDeployState(await res.json());
    } catch { /* ignore */ }
  };

  const handlePausePipeline = async () => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/deploy-state/pause`, {
        method: 'POST', headers: await authHeaders(),
      });
      if (!res.ok) throw new Error(res.statusText);
      addToast('Pipeline paused', 'success');
      fetchDeployState();
    } catch {
      addToast('Failed to pause pipeline', 'error');
    }
  };

  const handleResumePipeline = async () => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/deploy-state/resume`, {
        method: 'POST', headers: await authHeaders(),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        addToast(data.detail || 'Failed to resume pipeline', 'error');
        return;
      }
      addToast('Pipeline resumed', 'success');
      fetchDeployState();
      fetchTasks();
    } catch {
      addToast('Failed to resume pipeline', 'error');
    }
  };

  const handleRollback = async () => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/deploy-state/rollback`, {
        method: 'POST', headers: await authHeaders(),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        addToast(`Rollback failed: ${data.detail || res.statusText}`, 'error');
        return;
      }
      const data = await res.json();
      addToast(`Rolled back: ${data.from_sha?.slice(0, 8)} → ${data.to_sha?.slice(0, 8)}`, 'success');
      fetchDeployState();
    } catch {
      addToast('Rollback failed', 'error');
    }
  };

  const handleMarkStable = async () => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/deploy-state/mark-stable`, {
        method: 'POST', headers: await authHeaders(),
      });
      if (!res.ok) throw new Error(res.statusText);
      addToast('Deploy marked stable', 'success');
      fetchDeployState();
    } catch {
      addToast('Failed to mark stable', 'error');
    }
  };

  const handleRepairState = async () => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/repair-state`, {
        method: 'POST', headers: await authHeaders(),
      });
      if (!res.ok) throw new Error(res.statusText);
      const data = await res.json();
      addToast(`Repaired ${data.count} issue${data.count !== 1 ? 's' : ''}`, 'success');
      fetchTasks();
    } catch {
      addToast('State repair failed', 'error');
    }
  };

  // Fetch deploy state on mount and periodically
  useEffect(() => {
    fetchDeployState();
    const id = setInterval(fetchDeployState, 30000);
    return () => clearInterval(id);
  }, []);

  // --- Zone Health ---
  const [zoneHealth, setZoneHealth] = useState<Record<string, { status: string; last_success: string | null; last_failure: string | null; last_failure_reason: string | null; consecutive_failures: number; cooldown_active: boolean }> | null>(null);

  const fetchZoneHealth = async () => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/zone-health`);
      if (res.ok) setZoneHealth(await res.json());
    } catch { /* ignore */ }
  };

  useEffect(() => {
    fetchZoneHealth();
    const id = setInterval(fetchZoneHealth, 30000);
    return () => clearInterval(id);
  }, []);

  // --- Orchestrator Health ---
  interface OrchestratorHealth {
    last_cycle_seconds_ago: number | null;
    queued_tasks: number;
    active_tasks: number;
    pool_info: Record<string, unknown>;
    deploy_monitoring?: {
      external_health?: Record<string, boolean | string | string[]>;
    };
  }
  const [orchestratorHealth, setOrchestratorHealth] = useState<OrchestratorHealth | null>(null);

  const fetchOrchestratorHealth = async () => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/debug/orchestrator`);
      if (res.ok) setOrchestratorHealth(await res.json());
      else setOrchestratorHealth(null);
    } catch { setOrchestratorHealth(null); }
  };

  useEffect(() => {
    fetchOrchestratorHealth();
    const id = setInterval(fetchOrchestratorHealth, 30000);
    return () => clearInterval(id);
  }, []);

  const handleBulkArchive = async (filter: 'completed' | 'terminal') => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks/bulk-archive`, {
        method: 'POST',
        headers: { ...(await authHeaders()), 'Content-Type': 'application/json' },
        body: JSON.stringify({ filter }),
      });
      if (!res.ok) throw new Error('Request failed');
      const data = await res.json();
      fetchTasks();
      addToast(`Archived ${data.archived_count} task${data.archived_count !== 1 ? 's' : ''}`, 'success');
    } catch (error) {
      console.error('Failed to bulk archive tasks:', error);
      addToast('Failed to archive tasks', 'error');
    }
    setConfirmDialog(null);
  };

  const handleBulkArchiveSelected = async () => {
    const ids = Array.from(selectedTaskIds);
    if (ids.length === 0) return;
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks/bulk-archive`, {
        method: 'POST',
        headers: { ...(await authHeaders()), 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_ids: ids }),
      });
      if (!res.ok) throw new Error('Request failed');
      const data = await res.json();
      setSelectedTaskIds(new Set());
      setSelectionMode(false);
      fetchTasks();
      addToast(`Archived ${data.archived_count} task${data.archived_count !== 1 ? 's' : ''}`, 'success');
    } catch (error) {
      console.error('Failed to archive selected tasks:', error);
      addToast('Failed to archive tasks', 'error');
    }
    setConfirmDialog(null);
  };

  const handleBulkArchiveColumn = async (taskIds: string[]) => {
    if (taskIds.length === 0) return;
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks/bulk-archive`, {
        method: 'POST',
        headers: { ...(await authHeaders()), 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_ids: taskIds }),
      });
      if (!res.ok) throw new Error('Request failed');
      const data = await res.json();
      fetchTasks();
      addToast(`Archived ${data.archived_count} task${data.archived_count !== 1 ? 's' : ''}`, 'success');
    } catch (error) {
      console.error('Failed to archive column tasks:', error);
      addToast('Failed to archive tasks', 'error');
    }
    setConfirmDialog(null);
  };

  const toggleSelectionMode = () => {
    setSelectionMode(prev => !prev);
    setSelectedTaskIds(new Set());
  };

  const toggleTaskSelection = (taskId: string) => {
    setSelectedTaskIds(prev => {
      const next = new Set(prev);
      if (next.has(taskId)) {
        next.delete(taskId);
      } else {
        next.add(taskId);
      }
      return next;
    });
  };

  const bulkColumnToggle = (taskIds: string[], selected: boolean) => {
    setSelectedTaskIds(prev => {
      const next = new Set(prev);
      for (const id of taskIds) {
        if (selected) {
          next.add(id);
        } else {
          next.delete(id);
        }
      }
      return next;
    });
  };

  const fetchSettings = async () => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/settings`);
      if (res.ok) {
        const data = await res.json();
        setMaxConcurrentVMs(data.max_concurrent_vms);
        setMaxRetries(data.max_retries ?? 3);
        setMaxAutonomousTasksPerHour(data.max_autonomous_tasks_per_hour ?? 10);
        setAutonomousResearchIntervalMinutes(data.autonomous_research_interval_minutes ?? 30);
        setAutonomousResearchModel(data.autonomous_research_model ?? '');
        setAutonomousResearchEffort(data.autonomous_research_effort ?? '');
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
      } else {
        addToast('Failed to add account', 'error');
      }
    } catch (err) { console.error('Failed to add account:', err); addToast('Failed to add account', 'error'); }
  };

  const removeMaxPlanAccount = async (id: string) => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/max-plan-accounts/${id}`, {
        method: 'DELETE', headers: await authHeaders(),
      });
      if (res.ok) {
        fetchSettings();
      } else {
        addToast('Failed to remove account', 'error');
      }
    } catch (err) { console.error('Failed to remove account:', err); addToast('Failed to remove account', 'error'); }
  };

  const toggleMaxPlanAccount = async (id: string, enabled: boolean) => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/max-plan-accounts/${id}`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ enabled }),
      });
      if (res.ok) {
        fetchSettings();
      } else {
        addToast('Failed to update account', 'error');
      }
    } catch (err) { console.error('Failed to toggle account:', err); addToast('Failed to update account', 'error'); }
  };

  const connectGitHub = async () => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/github/oauth/authorize`);
      if (res.ok) {
        const { url } = await res.json();
        window.location.href = url;
      } else {
        addToast('Failed to start GitHub connection', 'error');
      }
    } catch (err) { console.error('Failed to start GitHub OAuth:', err); addToast('Failed to start GitHub connection', 'error'); }
  };

  const syncTaskTypeDefaults = async () => {
    const types = ['create', 'plan', 'research', 'review', 'fix', 'security_audit', 'qa', 'deps', 'docs'];
    const models: Record<string, string> = {};
    const efforts: Record<string, string> = {};
    for (const t of types) {
      const m = JSON.parse(localStorage.getItem(`blueprint-admin:taskTypeModel:${t}`) ?? 'null') ?? '';
      const e = JSON.parse(localStorage.getItem(`blueprint-admin:taskTypeEffort:${t}`) ?? 'null') ?? '';
      if (m) models[t] = m;
      if (e) efforts[t] = e;
    }
    try {
      await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ task_type_models: models, task_type_efforts: efforts }),
      });
    } catch (err) {
      console.error('Failed to sync task type defaults:', err);
    }
  };

  const updateMaxVMs = async (value: number) => {
    const clamped = Math.max(1, Math.min(100, value));
    const prev = maxConcurrentVMs;
    setMaxConcurrentVMs(clamped);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ max_concurrent_vms: clamped }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (err) {
      console.error('Failed to update settings:', err);
      setMaxConcurrentVMs(prev);
      addToast('Failed to update max VMs', 'error');
    }
  };

  const updateMaxRetries = async (value: number) => {
    const v = Math.max(0, Math.min(10, value));
    const prev = maxRetries;
    setMaxRetries(v);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ max_retries: v }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      setMaxRetries(prev);
      addToast('Failed to update max retries', 'error');
    }
  };

  const handleUpdateMaxAutonomousRate = async (value: number) => {
    const v = Math.max(1, Math.min(50, value));
    const prev = maxAutonomousTasksPerHour;
    setMaxAutonomousTasksPerHour(v);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ max_autonomous_tasks_per_hour: v }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      setMaxAutonomousTasksPerHour(prev);
      addToast('Failed to update autonomous rate', 'error');
    }
  };

  const handleUpdateAutonomousResearchInterval = async (value: number) => {
    const v = Math.max(1, Math.min(1440, value));
    const prev = autonomousResearchIntervalMinutes;
    setAutonomousResearchIntervalMinutes(v);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ autonomous_research_interval_minutes: v }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      setAutonomousResearchIntervalMinutes(prev);
      addToast('Failed to update research interval', 'error');
    }
  };

  const handleUpdateAutonomousResearchModel = async (value: string) => {
    const prev = autonomousResearchModel;
    setAutonomousResearchModel(value);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ autonomous_research_model: value }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      setAutonomousResearchModel(prev);
      addToast('Failed to update research model', 'error');
    }
  };

  const handleUpdateAutonomousResearchEffort = async (value: string) => {
    const prev = autonomousResearchEffort;
    setAutonomousResearchEffort(value);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ autonomous_research_effort: value }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      setAutonomousResearchEffort(prev);
      addToast('Failed to update research effort', 'error');
    }
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

  const getDefaultModelForType = (taskType: string) => {
    return JSON.parse(localStorage.getItem(`blueprint-admin:taskTypeModel:${taskType}`) ?? 'null') ??
      JSON.parse(localStorage.getItem('blueprint-admin:defaultModel') ?? 'null') ?? '';
  };

  const getDefaultEffortForType = (taskType: string) => {
    return JSON.parse(localStorage.getItem(`blueprint-admin:taskTypeEffort:${taskType}`) ?? 'null') ??
      JSON.parse(localStorage.getItem('blueprint-admin:defaultEffort') ?? 'null') ?? '';
  };

  const openNewTaskModal = () => {
    setShowNewTaskModal(true);
    setNewTaskType(defaultTaskType);
    setNewTaskModel(getDefaultModelForType(defaultTaskType));
    setNewTaskEffort(getDefaultEffortForType(defaultTaskType));
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
      fetchTasks();
    } catch (error) {
      console.error('Failed to create task:', error);
      addToast('Failed to create task', 'error');
    }
  };

  const handleViewLogs = useCallback((task: Task) => {
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setLogTask(task);
    setLogLines(task.error_logs ? [`[Error Log]\n${task.error_logs}`] : []);
    // Fetch fresh task data in the background; show stale list data immediately
    fetchTask(task.id).then(fresh => setLogTask(fresh)).catch(() => {/* keep stale data */});
    setWsDisconnected(false);

    if (task.status !== 'IN_PROGRESS' && task.status !== 'PROVISIONING') return;
    if (!task.project_id) return;

    setLogsConnecting(true);

    (async () => {
      try {
        const ticketRes = await fetch(`${API_BASE_URL}/api/projects/${task.project_id}/ws-ticket`, {
          method: 'POST',
          headers: await authHeaders(),
        });
        if (!ticketRes.ok) {
          setLogsConnecting(false);
          setLogLines(prev => [...prev, '[Failed to get WebSocket ticket]']);
          return;
        }
        const { ticket } = await ticketRes.json();

        const wsUrl = (API_BASE_URL || window.location.origin).replace(/^http/, 'ws');
        const ws = new WebSocket(`${wsUrl}/api/projects/${task.project_id}/ws`);
        wsRef.current = ws;
        let wasConnected = false;

        ws.onopen = () => {
          ws.send(JSON.stringify({ ticket }));
          setLogLines(prev => [...prev, '[Connected to live log stream]']);
          setWsConnected(true);
          setLogsConnecting(false);
          wasConnected = true;
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
              fetchTasksDebounceRef.current = setTimeout(() => { fetchTasksRef.current(); }, 500);
            }
          } catch {
            // ignore parse errors
          }
        };

        ws.onerror = (e) => {
          console.error('[WS] error:', e);
          setLogLines(prev => [...prev, `[WebSocket error — check browser console (F12)]`]);
          setLogsConnecting(false);
          if (wasConnected) setWsDisconnected(true);
        };

        ws.onclose = (e) => {
          console.log('[WS] closed:', e.code, e.reason);
          setLogLines(prev => [...prev, `[Stream disconnected: code=${e.code} reason=${e.reason || 'none'}]`]);
          setWsConnected(false);
          setLogsConnecting(false);
          if (wasConnected) setWsDisconnected(true);
        };
      } catch (err) {
        setLogsConnecting(false);
        setLogLines(prev => [...prev, `[Connection error: ${err}]`]);
      }
    })();
  }, []);

  const closeLogViewer = () => {
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setWsConnected(false);
    setLogsConnecting(false);
    setWsDisconnected(false);
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
      let url = `${API_BASE_URL}/api/admin/tasks?limit=${ps}&offset=${offset}&archived=true`;
      if (status !== 'all') {
        url += `&status=${status}`;
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
      addToast('Failed to load archive', 'error');
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
    tasks, totalTasks, globalActiveCount, globalEvaluatingCount, globalQueuedCount, loading, page, setPage, limit, setLimit, refreshInterval, setRefreshInterval,
    actionLoadingMap,
    fetchTasks, handleApprove, handleClearWaveDeps, handleRequestChanges, handleCancel, handleRetry, handleEditPrompt, handleDismiss, handleArchiveTask, handleUnarchiveTask,
    handleBulkArchive, handleBulkArchiveSelected, handleBulkArchiveColumn,
    selectionMode, selectedTaskIds, toggleSelectionMode, toggleTaskSelection, bulkColumnToggle,
    confirmDialog, setConfirmDialog,
    // Toasts
    toasts, addToast, dismissToast,
    // Alerts
    alerts, dismissAlert,
    // View
    viewMode, setViewMode,
    compactMode, setCompactMode,
    groupBy, setGroupBy,
    // Column preferences
    columnPrefs, setColumnPrefs,
    // Search/filter
    searchQuery, setSearchQuery, taskTypeFilter, setTaskTypeFilter, statusFilter, setStatusFilter,
    // Projects
    projects, projectsFetchError, selectedProjectId, setSelectedProjectId, handlePauseAllAutonomous,
    // Log viewer
    logTask, logLines, logsConnecting, wsDisconnected, handleViewLogs, closeLogViewer,
    // Research browser
    showResearchBrowser, setShowResearchBrowser,
    // Activity feed
    showActivityFeed, setShowActivityFeed,
    // New task modal
    showNewTaskModal, setShowNewTaskModal,
    newTaskRepo, newTaskBranch, newTaskTitle, newTaskPrompt, newTaskType,
    newTaskPrNumber, newTaskPrBranch, newTaskModel, newTaskEffort,
    setNewTaskRepo, setNewTaskBranch, setNewTaskTitle, setNewTaskPrompt,
    setNewTaskType, setNewTaskPrNumber, setNewTaskPrBranch, setNewTaskModel, setNewTaskEffort,
    repoOptions, repoLoading, repoError, useManualRepo, setUseManualRepo,
    branchOptions, branchLoading, branchError, useManualBranch, setUseManualBranch, setBranchOptions,
    openNewTaskModal, handleRepoChange, handleCreateTask, getDefaultModelForType, getDefaultEffortForType,
    // Defaults
    defaultTaskType, setDefaultTaskType,
    notifyOnComplete, setNotifyOnComplete,
    soundOnComplete, setSoundOnComplete,
    // Settings
    showSettings, settingsRef, handleToggleSettings,
    maxConcurrentVMs, maxRetries, maxAutonomousTasksPerHour, usingMaxPlan,
    autonomousResearchIntervalMinutes, autonomousResearchModel, autonomousResearchEffort,
    updateMaxVMs, updateMaxRetries, handleUpdateMaxAutonomousRate, syncTaskTypeDefaults,
    handleUpdateAutonomousResearchInterval, handleUpdateAutonomousResearchModel, handleUpdateAutonomousResearchEffort,
    // Max plan accounts
    maxPlanAccounts, newAccountName, setNewAccountName, newAccountToken, setNewAccountToken,
    addMaxPlanAccount, removeMaxPlanAccount, toggleMaxPlanAccount,
    // Orchestrator
    handleRestartOrchestrator,
    // GitHub
    githubAccount, connectGitHub,
    // Directions
    showDirections, setShowDirections,
    // Metrics
    showMetrics, setShowMetrics, metricsHistory, cumulativeCost, cumulativeVmHours, metricsWindow, setMetricsWindow,
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
    // Deploy state
    deployState, handlePausePipeline, handleResumePipeline,
    handleRollback, handleMarkStable, handleRepairState,
    // Zone health
    zoneHealth,
    // Orchestrator health
    orchestratorHealth,
  };
}
