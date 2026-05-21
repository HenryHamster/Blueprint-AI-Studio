import { useState, useEffect, useRef, useMemo } from 'react';
import type { Task } from './types/task';
import type { Project, AutoRemediationConfig, AutoRemediationStats, IssueImplementationConfig, AutoReviewConfig } from './types/project';
import type { Alert } from './types/alert';
import { API_BASE_URL, apiFetch, authHeaders, authGet } from './api';
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
import { useSettings } from './hooks/useSettings';
import { useUserManagement } from './hooks/useUserManagement';
import { useProjectMembers } from './hooks/useProjectMembers';

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

  const { searchQuery, setSearchQuery, taskTypeFilter, setTaskTypeFilter, statusFilter, setStatusFilter, dateRange, setDateRange, blockedFilter, setBlockedFilter, requiresApproval, setRequiresApproval, stuckFilter, setStuckFilter, stuckMinSeconds, setStuckMinSeconds } = useSearchFilters();

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
    toggleSelectionMode, toggleTaskSelection, bulkColumnToggle, shiftSelectRange,
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

  const {
    maxConcurrentVMs, maxRetries, maxAutonomousTasksPerHour,
    autonomousResearchIntervalMinutes, directionCooldownMinutes, strategicResearchFrequency,
    healthCheckUrls, agentPromptSuffixes, usingMaxPlan,
    maxPlanAccounts, newAccountName, setNewAccountName, newAccountProvider, setNewAccountProvider,
    newAccountAuthMode, setNewAccountAuthMode, newAccountToken, setNewAccountToken,
    newAccountProviderMetadata, setNewAccountProviderMetadata, newAccountClaudeConfigJson, setNewAccountClaudeConfigJson,
    newAccountDailyTaskCap, setNewAccountDailyTaskCap,
    newAccountMaxConcurrentTasks, setNewAccountMaxConcurrentTasks,
    newAccountAutoDisableAfterRateLimits, setNewAccountAutoDisableAfterRateLimits,
    githubAccount, setGithubAccount,
    requireApprovalGates,
    autoArchiveEnabled, autoArchiveTerminalAfterDays,
    autoArchiveMaxTerminalTasksPerProject, autoArchiveIntervalMinutes,
    notificationConfig, dailyCostCeilingUsd,
    providerTierModelMap, taskTypeModelPreferences,
    taskTypeCategories, categoryRouting, taskTypeRouting,
    providerSettingsByEffort,
    providerSelectionMode,
    crossProviderFallbackEnabled, providers,
    providerSettingsDefaults, taskTypeProviderOverrides,
    buildConfig, setBuildConfig,
    fetchSettings,
    addMaxPlanAccount, removeMaxPlanAccount, toggleMaxPlanAccount, resetAccountRateLimits,
    refreshAccountModels,
    updateAccountCaps,
    connectGitHub,
    updateAgentPromptSuffixes,
    updateProviderTierModels, updateTaskTypeModelPreferenceConfig,
    updateTaskTypeCategories, updateCategoryRouting, updateTaskTypeRouting,
    updateProviderSettingsByEffort,
    updateProviderSelectionMode,
    updateCrossProviderFallback,
    updateProviderSettingsDefaults, updateTaskTypeProviderOverrides,
    updateMaxVMs, updateMaxRetries, updateRequireApprovalGates,
    updateAutoArchiveEnabled, updateAutoArchiveTerminalAfterDays,
    updateAutoArchiveMaxTerminalTasksPerProject, updateAutoArchiveIntervalMinutes,
    handleUpdateMaxAutonomousRate, handleUpdateAutonomousResearchInterval,
    handleUpdateDirectionCooldownMinutes, handleUpdateStrategicResearchFrequency,
    updateHealthCheckUrls, updateNotificationConfig, updateDailyCostCeiling, updateBuildConfig,
  } = useSettings(addToast);

  const {
    allowedUsers, addAllowedUser, updateAllowedUserRole, removeAllowedUser,
  } = useUserManagement(showUserManagement);

  const {
    projectMembers, fetchProjectMembers, addProjectMember, updateProjectMemberRole, removeProjectMember,
  } = useProjectMembers();

  // ── Remaining state ─────────────────────────────────────────────────────
  const [tasks, setTasks] = useState<Task[]>([]);
  const [totalTasks, setTotalTasks] = useState(0);
  const [globalActiveCount, setGlobalActiveCount] = useState(0);
  const [globalEvaluatingCount, setGlobalEvaluatingCount] = useState(0);
  const [globalQueuedCount, setGlobalQueuedCount] = useState(0);
  const [initialLoading, setInitialLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [page, setPage] = useState(1);
  const [rawLimit, setLimit] = usePersistedState<number>('pageSize', 50);
  // Backend caps /admin/tasks at 500 rows to keep response under Cloud Run's
  // 32 MB body limit; clamp stale localStorage values so pagination offsets
  // stay consistent with what the server actually returns.
  const limit = Math.min(rawLimit, 500);
  const [refreshInterval, setRefreshInterval] = usePersistedState<number>('refreshInterval', 30000);

  const [actionLoadingMap, setActionLoadingMap] = useState<Record<string, string>>({});
  const [alerts, setAlerts] = useState<Alert[]>([]);

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

  // Sync buildConfig from selected project whenever selection or project list changes
  useEffect(() => {
    const project = projects.find(p => p.id === selectedProjectId) ?? null;
    setBuildConfig(project?.build_config ?? null);
  }, [selectedProjectId, projects]); // eslint-disable-line react-hooks/exhaustive-deps

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
        if (selectionMode) {
          if (selectedTaskIds.size > 0) { setSelectedTaskIds(new Set()); } else { toggleSelectionMode(); }
          return;
        }
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
          if (selectionMode && selectedTaskIds.size > 0) {
            const failed = tasks.filter(t => selectedTaskIds.has(t.id) && t.status === 'FAILED');
            if (failed.length > 0) {
              setConfirmDialog({
                title: `Retry ${failed.length} task${failed.length !== 1 ? 's' : ''}?`,
                message: failed.map(t => t.title || t.id).join('\n'),
                confirmLabel: `Retry ${failed.length}`,
                onConfirm: () => {
                  handleBulkAction('retry', failed.map(t => t.id));
                  toggleSelectionMode();
                  setConfirmDialog(null);
                },
              });
            }
          } else if (!showNewTaskModal && !logTask && !showResearchBrowser && !showDirections && !showSettings) {
            fetchTasks();
          }
          break;
        case 'Delete':
          if (selectionMode && selectedTaskIds.size > 0) {
            const cancellable = tasks.filter(t => selectedTaskIds.has(t.id) && ['QUEUED', 'IN_PROGRESS', 'PROVISIONING', 'EVALUATING'].includes(t.status));
            if (cancellable.length > 0) {
              setConfirmDialog({
                title: `Cancel ${cancellable.length} task${cancellable.length !== 1 ? 's' : ''}?`,
                message: cancellable.map(t => t.title || t.id).join('\n'),
                confirmLabel: `Cancel ${cancellable.length}`,
                onConfirm: () => handleBulkCancel(cancellable.map(t => t.id)),
              });
            }
          }
          break;
        case 'Enter':
          if (selectionMode && selectedTaskIds.size > 0) {
            const ids = Array.from(selectedTaskIds);
            const taskId = ids[ids.length - 1];
            const task = tasks.find(t => t.id === taskId);
            if (task) { handleViewLogs(task); }
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
  }, [showNewTaskModal, logTask, showResearchBrowser, showActivityFeed, showMessages, showDirections, showMetrics, showSettings, showShortcutHelp, showArchive, selectionMode, selectedTaskIds, tasks]);

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
      const PAGE_LIMIT = 50;
      const allItems: unknown[] = [];
      let offset = 0;
      let hasMore = true;
      while (hasMore) {
        const res = await authGet(`${API_BASE_URL}/api/admin/projects?limit=${PAGE_LIMIT}&offset=${offset}`);
        if (!res.ok) {
          setProjectsFetchError('Failed to load projects');
          if (!silent) addToast('Failed to load projects', 'error');
          return;
        }
        const data = await res.json() as { items: unknown[]; has_more: boolean };
        allItems.push(...(data.items || []));
        hasMore = data.has_more ?? false;
        offset += PAGE_LIMIT;
      }
      setProjects(allItems as never[]);
      setProjectsFetchError(null);
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
        if (original) setTasks(prev => prev.map(t => t.id === taskId ? original : t));
        addToast(data.detail ? `Failed to approve task: ${data.detail}` : 'Failed to approve task', 'error');
        return;
      }
      fetchTasks();
      addToast('Task approved', 'success');
    } catch (error) {
      console.error('Failed to approve task:', error);
      if (original) setTasks(prev => prev.map(t => t.id === taskId ? original : t));
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
        if (original) setTasks(prev => prev.map(t => t.id === taskId ? original : t));
        addToast(data.detail ? `Failed to cancel task: ${data.detail}` : 'Failed to cancel task', 'error');
        return;
      }
      fetchTasks();
      addToast('Task cancelled', 'success');
    } catch (error) {
      console.error('Failed to cancel task:', error);
      if (original) setTasks(prev => prev.map(t => t.id === taskId ? original : t));
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
      if (original) setTasks(prev => prev.map(t => t.id === taskId ? original : t));
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
      // Also refresh the main task list so pipeline/kanban pick up the
      // now-unarchived task (used by pipeline-view "Unarchive parent").
      fetchTasks();
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
    // Chunk to avoid large single requests that can hit Cloud Run request timeouts.
    const CHUNK_SIZE = 100;
    try {
      let totalArchived = 0;
      for (let i = 0; i < taskIds.length; i += CHUNK_SIZE) {
        const chunk = taskIds.slice(i, i + CHUNK_SIZE);
        const res = await apiFetch('/api/admin/tasks/bulk-archive', {
          method: 'POST',
          body: JSON.stringify({ task_ids: chunk }),
        });
        if (!res.ok) throw new Error(`Request failed (${res.status})`);
        const data = await res.json();
        totalArchived += data.archived_count ?? 0;
      }
      fetchTasks();
      addToast(`Archived ${totalArchived} task${totalArchived !== 1 ? 's' : ''}`, 'success');
    } catch (error) {
      console.error('Failed to archive column tasks:', error);
      addToast('Failed to archive tasks', 'error');
    }
    setConfirmDialog(null);
  };

  const handleBulkCancel = async (taskIds: string[]) => {
    if (taskIds.length === 0) return;
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks/bulk-action`, {
        method: 'POST',
        headers: { ...(await authHeaders()), 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'cancel', task_ids: taskIds }),
      });
      if (!res.ok) throw new Error('Request failed');
      const data = await res.json();
      setSelectedTaskIds(new Set());
      setSelectionMode(false);
      fetchTasks();
      if (data.error_count === 0) {
        addToast(`Cancelled ${data.success_count} task${data.success_count !== 1 ? 's' : ''}`, 'success');
      } else {
        addToast(`${data.success_count} succeeded, ${data.error_count} failed`, 'error');
      }
    } catch (error) {
      console.error('Failed to bulk cancel tasks:', error);
      addToast('Failed to cancel tasks', 'error');
    }
    setConfirmDialog(null);
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

  const handleUpdateBuildConfig = async (config: import('./types/project').BuildConfig) => {
    if (!selectedProjectId) return;
    try {
      await updateBuildConfig(selectedProjectId, config);
      addToast('Build config saved', 'success');
      fetchProjects(true);
    } catch {
      addToast('Failed to save build config', 'error');
    }
  };

  const handleDetectBuildConfig = async () => {
    if (!selectedProjectId) return;
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/detect-build-config`, {
        method: 'POST',
        headers: await authHeaders(),
      });
      if (!res.ok) {
        let msg = 'Detection failed';
        try {
          const err = await res.json();
          if (err?.detail) msg = err.detail;
        } catch { /* ignore parse errors */ }
        addToast(msg, 'error');
        return;
      }
      const data = await res.json();
      setBuildConfig(data.build_config ?? null);
      addToast('Build config detected', 'success');
      fetchProjects(true);
    } catch {
      addToast('Detection failed', 'error');
    }
  };

  // ── Auto-remediation config (project-scoped) ────────────────────────────
  const [autoRemediationConfig, setAutoRemediationConfig] = useState<AutoRemediationConfig | null>(null);
  const [autoRemediationStats, setAutoRemediationStats] = useState<AutoRemediationStats | null>(null);

  useEffect(() => {
    if (!selectedProjectId) {
      setAutoRemediationConfig(null);
      setAutoRemediationStats(null);
      return;
    }
    authGet(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/auto-remediation`)
      .then(res => res.ok ? res.json() : null)
      .then(data => {
        if (data?.config) setAutoRemediationConfig(data.config);
        if (data?.stats) setAutoRemediationStats(data.stats);
      })
      .catch(() => {});
  }, [selectedProjectId]);

  const handleUpdateAutoRemediationConfig = async (config: AutoRemediationConfig) => {
    if (!selectedProjectId) return;
    const prev = autoRemediationConfig;
    setAutoRemediationConfig(config);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/auto-remediation`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify(config),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setAutoRemediationConfig(data.config);
      addToast('Auto-remediation config saved', 'success');
    } catch {
      setAutoRemediationConfig(prev);
      addToast('Failed to save auto-remediation config', 'error');
    }
  };

  // ── Issue implementation config (project-scoped) ────────────────────────
  const [issueImplementationConfig, setIssueImplementationConfig] = useState<IssueImplementationConfig | null>(null);

  useEffect(() => {
    if (!selectedProjectId) {
      setIssueImplementationConfig(null);
      return;
    }
    authGet(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/issue-implementation`)
      .then(res => res.ok ? res.json() : null)
      .then(data => { if (data?.config) setIssueImplementationConfig(data.config); })
      .catch(() => {});
  }, [selectedProjectId]);

  const handleUpdateIssueImplementationConfig = async (config: IssueImplementationConfig) => {
    if (!selectedProjectId) return;
    const prev = issueImplementationConfig;
    setIssueImplementationConfig(config);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/issue-implementation`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify(config),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setIssueImplementationConfig(data.config);
      addToast('Issue implementation config saved', 'success');
    } catch {
      setIssueImplementationConfig(prev);
      addToast('Failed to save issue implementation config', 'error');
    }
  };

  // ── Auto-review config (project-scoped) ─────────────────────────────────
  const [autoReviewConfig, setAutoReviewConfig] = useState<AutoReviewConfig | null>(null);

  useEffect(() => {
    if (!selectedProjectId) {
      setAutoReviewConfig(null);
      return;
    }
    authGet(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/auto-review`)
      .then(res => res.ok ? res.json() : null)
      .then(data => { if (data?.config) setAutoReviewConfig(data.config); })
      .catch(() => {});
  }, [selectedProjectId]);

  const handleUpdateAutoReviewConfig = async (patch: Partial<AutoReviewConfig>) => {
    if (!selectedProjectId || Object.keys(patch).length === 0) return;
    const prev = autoReviewConfig;
    setAutoReviewConfig(prev ? { ...prev, ...patch } : prev);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/auto-review`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify(patch),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setAutoReviewConfig(data.config);
      addToast('Auto-review config saved', 'success');
    } catch {
      setAutoReviewConfig(prev);
      addToast('Failed to save auto-review config', 'error');
    }
  };

  return {
    // Auth
    user, authLoading, userRole, accessDenied, error, setError,
    // Tasks
    tasks, totalTasks, globalActiveCount, globalEvaluatingCount, globalQueuedCount, initialLoading, refreshing, page, setPage, limit, setLimit, refreshInterval, setRefreshInterval,
    needsAttentionCount,
    actionLoadingMap,
    fetchTasks, handleApprove, handleClearWaveDeps, handleRequestChanges, handleCancel, handleRetry, handleEditPrompt, handleDismiss, handleArchiveTask, handleUnarchiveTask,
    handleBulkArchive, handleBulkArchiveSelected, handleBulkArchiveColumn, handleBulkAction, handleBulkCancel,
    selectionMode, setSelectionMode, selectedTaskIds, toggleSelectionMode, toggleTaskSelection, bulkColumnToggle, shiftSelectRange,
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
    searchQuery, setSearchQuery, taskTypeFilter, setTaskTypeFilter, statusFilter, setStatusFilter, dateRange, setDateRange, blockedFilter, setBlockedFilter, requiresApproval, setRequiresApproval, stuckFilter, setStuckFilter, stuckMinSeconds, setStuckMinSeconds,
    // Projects
    projects, projectsFetchError, selectedProjectId, setSelectedProjectId, handlePauseAllAutonomous, fetchProjects,
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
    updateMaxVMs, updateMaxRetries, updateRequireApprovalGates, handleUpdateMaxAutonomousRate,
    requireApprovalGates,
    autoArchiveEnabled, autoArchiveTerminalAfterDays,
    autoArchiveMaxTerminalTasksPerProject, autoArchiveIntervalMinutes,
    updateAutoArchiveEnabled, updateAutoArchiveTerminalAfterDays,
    updateAutoArchiveMaxTerminalTasksPerProject, updateAutoArchiveIntervalMinutes,
    agentPromptSuffixes, updateAgentPromptSuffixes,
    handleUpdateAutonomousResearchInterval, handleUpdateDirectionCooldownMinutes, handleUpdateStrategicResearchFrequency,
    healthCheckUrls, updateHealthCheckUrls,
    notificationConfig, updateNotificationConfig, dailyCostCeilingUsd, updateDailyCostCeiling,
    buildConfig, handleUpdateBuildConfig, handleDetectBuildConfig,
    autoRemediationConfig, autoRemediationStats, handleUpdateAutoRemediationConfig,
    issueImplementationConfig, handleUpdateIssueImplementationConfig,
    autoReviewConfig, handleUpdateAutoReviewConfig,
    // Max plan accounts
    maxPlanAccounts, newAccountName, setNewAccountName, newAccountProvider, setNewAccountProvider,
    newAccountAuthMode, setNewAccountAuthMode, newAccountToken, setNewAccountToken,
    newAccountProviderMetadata, setNewAccountProviderMetadata, newAccountClaudeConfigJson, setNewAccountClaudeConfigJson,
    newAccountDailyTaskCap, setNewAccountDailyTaskCap,
    newAccountMaxConcurrentTasks, setNewAccountMaxConcurrentTasks,
    newAccountAutoDisableAfterRateLimits, setNewAccountAutoDisableAfterRateLimits,
    addMaxPlanAccount, removeMaxPlanAccount, toggleMaxPlanAccount, resetAccountRateLimits, refreshAccountModels, updateAccountCaps,
    providerTierModelMap, taskTypeModelPreferences,
    taskTypeCategories, categoryRouting, taskTypeRouting,
    providerSettingsByEffort,
    providerSelectionMode,
    crossProviderFallbackEnabled, providers,
    providerSettingsDefaults, taskTypeProviderOverrides,
    updateProviderTierModels, updateTaskTypeModelPreferenceConfig,
    updateTaskTypeCategories, updateCategoryRouting, updateTaskTypeRouting,
    updateProviderSettingsByEffort,
    updateProviderSelectionMode,
    updateCrossProviderFallback,
    updateProviderSettingsDefaults, updateTaskTypeProviderOverrides,
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
