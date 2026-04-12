import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import type { Task } from './types/task';
import type { Project } from './types/project';
import type { MaxPlanAccount } from './types/settings';
import type { Alert } from './types/alert';
import { API_BASE_URL, authHeaders, authGet, apiFetch } from './api';
import { useToasts } from './components/Toasts';
import { STATUS_OPTIONS } from './constants';
import { usePersistedState } from './hooks/usePersistedState';
import { useAuth } from './hooks/useAuth';
import { useViewPreferences } from './hooks/useViewPreferences';
import { useMetrics } from './hooks/useMetrics';
import { useArchive } from './hooks/useArchive';
import { useDeploymentState } from './hooks/useDeploymentState';
import { useLogViewer } from './hooks/useLogViewer';
import { useSearchFilters } from './hooks/useSearchFilters';
import { useUIState } from './hooks/useUIState';
import { useTaskModal } from './hooks/useTaskModal';

export type { DeployState } from './types/settings';

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
  // ── Composed hooks ──────────────────────────────────────────────────────
  const { user, authLoading, userRole, accessDenied, error, setError } = useAuth();
  const { toasts, addToast, dismissToast } = useToasts();
  const { viewMode, setViewMode, compactMode, setCompactMode, groupBy, setGroupBy, columnPrefs, setColumnPrefs, defaultTaskType, setDefaultTaskType } = useViewPreferences();
  const { metricsHistory, cumulativeCost, cumulativeVmHours, metricsWindow, setMetricsWindow, addMetricsPoint } = useMetrics();
  const {
    showArchive, setShowArchive, archiveTasks, archiveLoading,
    archivePage, setArchivePage, archivePageSize, setArchivePageSize,
    archiveStatusFilter, setArchiveStatusFilter,
    archiveSearchQuery, setArchiveSearchQuery,
    archiveDateRange, setArchiveDateRange,
    archiveTotalTasks, fetchArchiveTasks,
    archiveSortOrder, setArchiveSortOrder,
  } = useArchive(addToast);

  const fetchTasksRef = useRef<() => Promise<void>>(async () => {});

  // Moved before useDeploymentState so it can be passed as a parameter
  const [selectedProjectId, setSelectedProjectId] = usePersistedState<string | null>('selectedProject', null);

  const {
    deployState, setDeployState,
    handlePausePipeline, handleResumePipeline, handleResetHealth,
    handleRollback, handleMarkStable, handleRepairState,
    zoneHealth, orchestratorHealth, detailedHealth, diagnostics,
  } = useDeploymentState(addToast, user, fetchTasksRef, selectedProjectId);

  const {
    logTask, logLines, logsConnecting, wsDisconnected, wsDisconnectReason,
    handleViewLogs, closeLogViewer,
    wsRef, wsConnected,
    pipelineThrottle,
    trackedTaskIds, trackedLogs, trackTask, untrackTask, untrackAll,
  } = useLogViewer({ fetchTasksRef, setDeployState });

  const { searchQuery, setSearchQuery, taskTypeFilter, setTaskTypeFilter, statusFilter, setStatusFilter } = useSearchFilters();

  const {
    showResearchBrowser, setShowResearchBrowser,
    showActivityFeed, setShowActivityFeed, lastActivityFeedOpen, prevLastActivityFeedOpen,
    showMessages, setShowMessages,
    showSettings, setShowSettings,
    showDirections, setShowDirections,
    showMetrics, setShowMetrics,
    showDiagnostics, setShowDiagnostics,
    showShortcutHelp, setShowShortcutHelp,
    showUserManagement, setShowUserManagement,
    settingsRef, shortcutHelpRef,
    selectionMode, setSelectionMode, selectedTaskIds, setSelectedTaskIds,
    toggleSelectionMode, toggleTaskSelection, bulkColumnToggle,
    confirmDialog, setConfirmDialog,
  } = useUIState();

  const {
    showNewTaskModal, setShowNewTaskModal,
    newTaskRepo, newTaskBranch, newTaskTitle, newTaskPrompt, newTaskType,
    newTaskPrNumber, newTaskPrBranch, newTaskModel, newTaskEffort,
    setNewTaskRepo, setNewTaskBranch, setNewTaskTitle, setNewTaskPrompt,
    setNewTaskType, setNewTaskPrNumber, setNewTaskPrBranch, setNewTaskModel, setNewTaskEffort,
    repoOptions, repoLoading, repoError, useManualRepo, setUseManualRepo,
    branchOptions, branchLoading, branchError, useManualBranch, setUseManualBranch, setBranchOptions,
    openNewTaskModal, handleRepoChange, handleCreateTask, getDefaultModelForType, getDefaultEffortForType,
  } = useTaskModal(defaultTaskType, addToast, fetchTasksRef);

  // ── Remaining state ─────────────────────────────────────────────────────
  const [tasks, setTasks] = useState<Task[]>([]);
  const [totalTasks, setTotalTasks] = useState(0);
  const [globalActiveCount, setGlobalActiveCount] = useState(0);
  const [globalEvaluatingCount, setGlobalEvaluatingCount] = useState(0);
  const [globalQueuedCount, setGlobalQueuedCount] = useState(0);
  const [initialLoading, setInitialLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [page, setPage] = useState(1);
  const [limit, setLimit] = usePersistedState<number>('pageSize', 50);
  const [refreshInterval, setRefreshInterval] = usePersistedState<number>('refreshInterval', 30000);

  const [actionLoadingMap, setActionLoadingMap] = useState<Record<string, string>>({});
  const [alerts, setAlerts] = useState<Alert[]>([]);

  // Settings state
  const [maxConcurrentVMs, setMaxConcurrentVMs] = useState(5);
  const [maxRetries, setMaxRetries] = useState(3);
  const [maxAutonomousTasksPerHour, setMaxAutonomousTasksPerHour] = useState(10);
  const [autonomousResearchIntervalMinutes, setAutonomousResearchIntervalMinutes] = useState(30);
  const [directionCooldownMinutes, setDirectionCooldownMinutes] = useState(240);
  const [strategicResearchFrequency, setStrategicResearchFrequency] = useState(6);
  const [healthCheckUrls, setHealthCheckUrls] = useState<string[]>([]);
  const [agentPromptSuffixes, setAgentPromptSuffixes] = useState<Record<string, string>>({});
  const [usingMaxPlan, setUsingMaxPlan] = useState(false);
  // Max plan pool state
  const [maxPlanAccounts, setMaxPlanAccounts] = useState<MaxPlanAccount[]>([]);
  const [newAccountName, setNewAccountName] = useState('');
  const [newAccountToken, setNewAccountToken] = useState('');

  // GitHub connection state
  const [githubAccount, setGithubAccount] = useState<string | null>(null);

  // Notification defaults
  const [notifyOnComplete, setNotifyOnComplete] = usePersistedState<boolean>('notifyOnComplete', false);
  const [notifyOnFail, setNotifyOnFail] = usePersistedState<boolean>('notifyOnFail', false);
  const [notifyOnApprovalNeeded, setNotifyOnApprovalNeeded] = usePersistedState<boolean>('notifyOnApprovalNeeded', false);
  const [soundOnComplete, setSoundOnComplete] = usePersistedState<boolean>('soundOnComplete', false);
  const prevTasksRef = useRef<Task[]>([]);
  const notifyRef = useRef(notifyOnComplete);
  useEffect(() => { notifyRef.current = notifyOnComplete; }, [notifyOnComplete]);
  const notifyOnFailRef = useRef(notifyOnFail);
  useEffect(() => { notifyOnFailRef.current = notifyOnFail; }, [notifyOnFail]);
  const notifyOnApprovalNeededRef = useRef(notifyOnApprovalNeeded);
  useEffect(() => { notifyOnApprovalNeededRef.current = notifyOnApprovalNeeded; }, [notifyOnApprovalNeeded]);
  const soundOnCompleteRef = useRef(soundOnComplete);
  useEffect(() => { soundOnCompleteRef.current = soundOnComplete; }, [soundOnComplete]);

  // Project dashboard state
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectsFetchError, setProjectsFetchError] = useState<string | null>(null);

  // Last visit timestamp — captures when the user previously loaded the app
  const [lastVisitTimestamp] = useState<number | null>(() => {
    try {
      const stored = localStorage.getItem('blueprint-admin:lastVisitTimestamp');
      const ts = stored ? parseInt(stored, 10) : null;
      // Update stored timestamp to now for next visit
      localStorage.setItem('blueprint-admin:lastVisitTimestamp', String(Date.now()));
      return ts && !isNaN(ts) ? ts : null;
    } catch { return null; }
  });

  // Refresh indicator state
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

  // Status bar state
  const [apiHealthy, setApiHealthy] = useState(true);
  const [lastFetchTime, setLastFetchTime] = useState<number>(0);

  // Reset page when status filter changes
  useEffect(() => { setPage(1); }, [statusFilter]);

  // ── Effects ─────────────────────────────────────────────────────────────

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
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

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
    const projectInterval = setInterval(() => fetchProjects(true), 60000);
    if (refreshInterval === 0) {
      return () => { clearInterval(projectInterval); };
    }
    const interval = setInterval(() => { fetchTasksRef.current(); fetchAlerts(true); }, refreshInterval);
    return () => { clearInterval(interval); clearInterval(projectInterval); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshInterval, user]);

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
        if (showMessages) { setShowMessages(false); return; }
        if (showArchive) { setShowArchive(false); return; }
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
  }, [showNewTaskModal, logTask, showResearchBrowser, showActivityFeed, showMessages, showDirections, showMetrics, showSettings, showShortcutHelp, showArchive]);

  // ── Functions ───────────────────────────────────────────────────────────

  const fetchTasks = async () => {
    if (tasks.length === 0) {
      setInitialLoading(true);
    } else {
      setRefreshing(true);
    }
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
          const prevApprovalMap = new Map(prevTasksRef.current.map(t => [t.id, t.requires_approval]));
          let didTransition = false;
          for (const t of newTasks) {
            const prev = prevMap.get(t.id);
            if (prev && prev !== t.status && (t.status === 'COMPLETED' || t.status === 'FAILED')) {
              if (t.status === 'COMPLETED' && notifyRef.current && Notification.permission === 'granted') {
                new Notification('Task completed', {
                  body: t.title || t.id,
                });
              }
              if (t.status === 'FAILED' && notifyOnFailRef.current && Notification.permission === 'granted') {
                new Notification('Task failed', {
                  body: t.title || t.id,
                });
              }
              didTransition = true;
            }
            // Approval-needed notification: task newly requires approval and is not in a terminal state
            const prevApproval = prevApprovalMap.get(t.id);
            const nonTerminal = t.status !== 'COMPLETED' && t.status !== 'FAILED' && t.status !== 'CANCELLED';
            if (!prevApproval && t.requires_approval && nonTerminal) {
              if (notifyOnApprovalNeededRef.current && Notification.permission === 'granted') {
                new Notification('Task requires approval', {
                  body: t.title || t.id,
                });
              }
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

        // Record metrics data point
        addMetricsPoint(data.active_count, newTasks, completedCount, failedCount);
      } else {
        setError(`Failed to fetch tasks: ${res.statusText}`);
        setApiHealthy(false);
      }
    } catch (err) {
      console.error('Failed to fetch tasks:', err);
      setError('Failed to connect to backend server.');
      setApiHealthy(false);
    } finally {
      setInitialLoading(false);
      setRefreshing(false);
    }
  };
  fetchTasksRef.current = fetchTasks;

  const fetchProjects = async (silent = false) => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/projects`);
      if (res.ok) {
        const data = await res.json();
        setProjects(data || []);
        setProjectsFetchError(null);
      } else {
        setProjectsFetchError('Failed to load projects');
        if (!silent) addToast('Failed to load projects', 'error');
      }
    } catch (err) {
      console.error('Failed to fetch projects:', err);
      setProjectsFetchError('Failed to load projects');
      if (!silent) addToast('Failed to load projects', 'error');
    }
  };

  const fetchAlerts = async (silent = false) => {
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
      if (!silent) addToast('Failed to load alerts', 'error');
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

  const dismissAllAlerts = async () => {
    const prev = alerts;
    setAlerts([]);
    try {
      const url = selectedProjectId
        ? `${API_BASE_URL}/api/admin/alerts/dismiss-all?project_id=${selectedProjectId}`
        : `${API_BASE_URL}/api/admin/alerts/dismiss-all`;
      const res = await fetch(url, {
        method: 'POST',
        headers: await authHeaders(),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (err) {
      console.error('Failed to dismiss all alerts:', err);
      setAlerts(prev);
      addToast('Failed to dismiss all alerts', 'error');
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

  const handleBulkAction = async (action: 'approve' | 'retry', taskIds: string[]) => {
    if (taskIds.length === 0) return;
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks/bulk-action`, {
        method: 'POST',
        headers: { ...(await authHeaders()), 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, task_ids: taskIds }),
      });
      if (!res.ok) throw new Error('Request failed');
      const data = await res.json();
      fetchTasks();
      if (data.error_count === 0) {
        addToast(`${action === 'approve' ? 'Approved' : 'Retried'} ${data.success_count} task${data.success_count !== 1 ? 's' : ''}`, 'success');
      } else {
        addToast(`${data.success_count} succeeded, ${data.error_count} failed`, 'error');
      }
    } catch (error) {
      console.error(`Failed to bulk ${action} tasks:`, error);
      addToast(`Failed to ${action} tasks`, 'error');
    }
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

  const fetchSettings = async () => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/settings`);
      if (res.ok) {
        const data = await res.json();
        setMaxConcurrentVMs(data.max_concurrent_vms);
        setMaxRetries(data.max_retries ?? 3);
        setMaxAutonomousTasksPerHour(data.max_autonomous_tasks_per_hour ?? 10);
        setAutonomousResearchIntervalMinutes(data.autonomous_research_interval_minutes ?? 30);
        setDirectionCooldownMinutes(data.autonomous_direction_cooldown_minutes ?? 240);
        setStrategicResearchFrequency(data.strategic_research_frequency ?? 6);
        setHealthCheckUrls(data.health_check_urls ?? []);
        setAgentPromptSuffixes(data.agent_prompt_suffixes ?? {});
        setUsingMaxPlan(data.using_max_plan ?? false);
        setMaxPlanAccounts(data.max_plan_accounts || []);
        setGithubAccount(data.github_account || null);
        // Seed localStorage with server-authoritative task type defaults
        const serverModels = (data.task_type_models ?? {}) as Record<string, string>;
        const serverEfforts = (data.task_type_efforts ?? {}) as Record<string, string>;
        for (const [t, m] of Object.entries(serverModels)) {
          localStorage.setItem(`blueprint-admin:taskTypeModel:${t}`, JSON.stringify(m));
        }
        for (const [t, e] of Object.entries(serverEfforts)) {
          localStorage.setItem(`blueprint-admin:taskTypeEffort:${t}`, JSON.stringify(e));
        }
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

  const resetAccountRateLimits = async (id: string) => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/max-plan-accounts/${id}/reset-rate-limits`, {
        method: 'POST', headers: await authHeaders(),
      });
      if (res.ok) {
        addToast('Rate limits cleared', 'success');
        fetchSettings();
      } else {
        addToast('Failed to reset rate limits', 'error');
      }
    } catch (err) { console.error('Failed to reset rate limits:', err); addToast('Failed to reset rate limits', 'error'); }
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

  const updateAgentPromptSuffixes = async (suffixes: Record<string, string>) => {
    const prev = agentPromptSuffixes;
    setAgentPromptSuffixes(suffixes);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ agent_prompt_suffixes: suffixes }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (err) {
      console.error('Failed to update agent prompt suffixes:', err);
      setAgentPromptSuffixes(prev);
      addToast('Failed to update prompt suffixes', 'error');
    }
  };

  const syncTaskTypeDefaults = async () => {
    const types = ['create', 'plan', 'research', 'review', 'fix', 'migrate', 'refactor', 'security_audit', 'qa', 'deps'];
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

  const handleUpdateDirectionCooldownMinutes = async (value: number) => {
    const v = Math.max(1, Math.min(10080, value));
    const prev = directionCooldownMinutes;
    setDirectionCooldownMinutes(v);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ autonomous_direction_cooldown_minutes: v }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      setDirectionCooldownMinutes(prev);
      addToast('Failed to update direction cooldown', 'error');
    }
  };

  const handleUpdateStrategicResearchFrequency = async (value: number) => {
    const v = Math.max(0, Math.min(100, value));
    const prev = strategicResearchFrequency;
    setStrategicResearchFrequency(v);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ strategic_research_frequency: v }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      setStrategicResearchFrequency(prev);
      addToast('Failed to update strategic research frequency', 'error');
    }
  };

  const updateHealthCheckUrls = async (urls: string[]) => {
    const prev = healthCheckUrls;
    setHealthCheckUrls(urls);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ health_check_urls: urls }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      addToast('Health check URLs saved', 'success');
    } catch {
      setHealthCheckUrls(prev);
      addToast('Failed to save health check URLs', 'error');
    }
  };

  const handleToggleSettings = () => {
    setShowSettings(prev => {
      if (!prev) fetchSettings();
      return !prev;
    });
  };

  const needsAttentionCount = useMemo(() => {
    return tasks.filter(t =>
      t.status === 'FAILED' ||
      (t.requires_approval && t.status !== 'COMPLETED' && t.status !== 'CANCELLED')
    ).length;
  }, [tasks]);

  // ── User management ──────────────────────────────────────────────────────
  type AllowedUserEntry = { id: string; email: string; role: string; display_name: string | null; last_login_at: string | null; created_at: string | null };
  const [allowedUsers, setAllowedUsers] = useState<AllowedUserEntry[]>([]);

  const fetchAllowedUsers = useCallback(async () => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/users`);
      if (res.ok) setAllowedUsers(await res.json());
    } catch { /* ignore */ }
  }, []);

  // Load users when panel opens
  useEffect(() => { if (showUserManagement) fetchAllowedUsers(); }, [showUserManagement, fetchAllowedUsers]);

  const addAllowedUser = useCallback(async (email: string, role: string) => {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/users`, {
      method: 'POST', body: JSON.stringify({ email, role }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(data.detail || 'Failed to add user');
    }
    await fetchAllowedUsers();
  }, [fetchAllowedUsers]);

  const updateAllowedUserRole = useCallback(async (userId: string, role: string) => {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/users/${userId}`, {
      method: 'PATCH', body: JSON.stringify({ role }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(data.detail || 'Failed to update user');
    }
    await fetchAllowedUsers();
  }, [fetchAllowedUsers]);

  const removeAllowedUser = useCallback(async (userId: string) => {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/users/${userId}`, { method: 'DELETE' });
    if (!res.ok) {
      const data = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(data.detail || 'Failed to remove user');
    }
    await fetchAllowedUsers();
  }, [fetchAllowedUsers]);

  // ── Project members ─────────────────────────────────────────────────────
  type ProjectMemberEntry = { id: string; user_id: string; email?: string; role: 'owner' | 'member' | 'viewer'; created_at: string | null };
  const [projectMembers, setProjectMembers] = useState<ProjectMemberEntry[]>([]);

  const fetchProjectMembers = useCallback(async (projectId: string) => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/projects/${projectId}/members`);
      if (res.ok) setProjectMembers(await res.json());
    } catch { /* ignore */ }
  }, []);

  const addProjectMember = useCallback(async (projectId: string, email: string, role: string) => {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/projects/${projectId}/members`, {
      method: 'POST', body: JSON.stringify({ email, role }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({ error: { message: res.statusText } }));
      throw new Error(data.error?.message || data.detail || 'Failed to add member');
    }
    await fetchProjectMembers(projectId);
  }, [fetchProjectMembers]);

  const updateProjectMemberRole = useCallback(async (projectId: string, memberId: string, role: string) => {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/projects/${projectId}/members/${memberId}`, {
      method: 'PATCH', body: JSON.stringify({ role }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({ error: { message: res.statusText } }));
      throw new Error(data.error?.message || data.detail || 'Failed to update role');
    }
    await fetchProjectMembers(projectId);
  }, [fetchProjectMembers]);

  const removeProjectMember = useCallback(async (projectId: string, memberId: string) => {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/projects/${projectId}/members/${memberId}`, {
      method: 'DELETE',
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({ error: { message: res.statusText } }));
      throw new Error(data.error?.message || data.detail || 'Failed to remove member');
    }
    await fetchProjectMembers(projectId);
  }, [fetchProjectMembers]);

  return {
    // Auth
    user, authLoading, userRole, accessDenied, error, setError,
    // Tasks
    tasks, totalTasks, globalActiveCount, globalEvaluatingCount, globalQueuedCount, initialLoading, refreshing, page, setPage, limit, setLimit, refreshInterval, setRefreshInterval,
    needsAttentionCount,
    actionLoadingMap,
    fetchTasks, handleApprove, handleClearWaveDeps, handleRequestChanges, handleCancel, handleRetry, handleEditPrompt, handleDismiss, handleArchiveTask, handleUnarchiveTask,
    handleBulkArchive, handleBulkArchiveSelected, handleBulkArchiveColumn, handleBulkAction,
    selectionMode, selectedTaskIds, toggleSelectionMode, toggleTaskSelection, bulkColumnToggle,
    confirmDialog, setConfirmDialog,
    // Toasts
    toasts, addToast, dismissToast,
    // Alerts
    alerts, dismissAlert, dismissAllAlerts,
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
    logTask, logLines, logsConnecting, wsDisconnected, wsDisconnectReason, handleViewLogs, closeLogViewer,
    // Research browser
    showResearchBrowser, setShowResearchBrowser,
    // Activity feed
    showActivityFeed, setShowActivityFeed, lastActivityFeedOpen, prevLastActivityFeedOpen,
    // Messages
    showMessages, setShowMessages,
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
    notifyOnFail, setNotifyOnFail,
    notifyOnApprovalNeeded, setNotifyOnApprovalNeeded,
    soundOnComplete, setSoundOnComplete,
    // Settings
    showSettings, settingsRef, handleToggleSettings,
    maxConcurrentVMs, maxRetries, maxAutonomousTasksPerHour, usingMaxPlan,
    autonomousResearchIntervalMinutes, directionCooldownMinutes, strategicResearchFrequency,
    updateMaxVMs, updateMaxRetries, handleUpdateMaxAutonomousRate, syncTaskTypeDefaults,
    agentPromptSuffixes, updateAgentPromptSuffixes,
    handleUpdateAutonomousResearchInterval, handleUpdateDirectionCooldownMinutes, handleUpdateStrategicResearchFrequency,
    healthCheckUrls, updateHealthCheckUrls,
    // Max plan accounts
    maxPlanAccounts, newAccountName, setNewAccountName, newAccountToken, setNewAccountToken,
    addMaxPlanAccount, removeMaxPlanAccount, toggleMaxPlanAccount, resetAccountRateLimits,
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
    lastVisitTimestamp,
    lastRefresh, wsRef,
    showShortcutHelp, setShowShortcutHelp, shortcutHelpRef,
    // Status bar
    apiHealthy, wsConnected, lastFetchTime,
    // Task tracking
    trackedTaskIds, trackedLogs, trackTask, untrackTask, untrackAll,
    // Deploy state
    deployState, handlePausePipeline, handleResumePipeline, handleResetHealth,
    handleRollback, handleMarkStable, handleRepairState,
    // Zone health
    zoneHealth,
    // Orchestrator health
    orchestratorHealth,
    detailedHealth,
    // Diagnostics
    diagnostics, showDiagnostics, setShowDiagnostics,
    pipelineThrottle,
    // User management
    allowedUsers, showUserManagement, setShowUserManagement,
    addAllowedUser, updateAllowedUserRole, removeAllowedUser,
    // Project members
    projectMembers, fetchProjectMembers, addProjectMember, updateProjectMemberRole, removeProjectMember,
  };
}
