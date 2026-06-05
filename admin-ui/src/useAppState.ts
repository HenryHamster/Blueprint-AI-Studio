import { useState, useEffect, useMemo, useRef } from 'react';
import type { Task } from './types/task';
import { API_BASE_URL, apiFetch, authHeaders, authGet } from './api';
import { useToasts } from './components/Toasts';
import { usePersistedState } from './hooks/usePersistedState';
import { usePoller } from './hooks/usePoller';
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
import { useNotificationPrefs } from './hooks/useNotificationPrefs';
import { useTasksState } from './hooks/useTasksState';
import { useProjectsState } from './hooks/useProjectsState';
import { useAlertsState } from './hooks/useAlertsState';
import { useProjectConfig } from './hooks/useProjectConfig';
import { resolveTaskCapabilities } from './taskCapabilities';

export type { DeployState } from './types/settings';

/**
 * useAppState is a THIN FACADE: it composes the per-domain hooks
 * (useTasksState / useProjectsState / useAlertsState / useProjectConfig /
 * useNotificationPrefs / useSettings / ...), wires the small amount of
 * cross-domain glue (shared refs, leading/polling fetches, keyboard
 * shortcuts, OAuth redirect handling, research feed) and re-exports the exact
 * same public return shape its ~140 consumers already depend on.
 *
 * Behavior is preserved end-to-end: the domain hooks own their own state and
 * callbacks; only the shape of the source file changed, not the runtime API.
 */
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

  // ── Shared refs (cross-domain glue owned by the facade) ──────────────────
  const fetchTasksRef = useRef<() => Promise<void>>(async () => {});
  // Populated by useDirectionsData (when the directions panel is mounted) so the
  // shared WS handler in useLogViewer can live-refresh the directions view.
  const fetchDirectionsRef = useRef<() => void>(() => {});
  const defaultTaskBranchRef = useRef<string | null>(null);
  const selectedProjectRepoRef = useRef<string | null>(null);
  const prevTasksRef = useRef<Task[]>([]);

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
    strategistActive,
    trackedTaskIds, trackedLogs, trackTask, untrackTask, untrackAll,
  } = useLogViewer({ fetchTasksRef, setDeployState, fetchDirectionsRef, selectedProjectId });

  const {
    showResearchBrowser, setShowResearchBrowser,
    showKnowledgeBrowser, setShowKnowledgeBrowser,
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
  const [researchFeed, setResearchFeed] = useState<Task[]>([]);

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
    maxGlobalAutonomousTasksPerHour, maxPipelineSlots, reservedSlotsForTypes,
    evaluatingPipelineTimeoutMinutes, evaluatingFailHours, ciBackstopTimeoutMinutes,
    updateMaxGlobalAutonomousRate, updateMaxPipelineSlots, updateReservedSlotsForTypes,
    updateEvaluatingPipelineTimeout, updateEvaluatingFailHours, updateCiBackstopTimeout,
    providerTierModelMap, taskTypeModelPreferences,
    taskTypeCategories, categoryRouting, taskTypeRouting,
    providerSettingsByEffort,
    providerSelectionMode,
    crossProviderFallbackEnabled, providers,
    taskCapabilityContract,
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
  const taskTypeFilterValues = useMemo(
    () => resolveTaskCapabilities(taskCapabilityContract).filterIds,
    [taskCapabilityContract],
  );
  const { searchQuery, setSearchQuery, taskTypeFilter, setTaskTypeFilter, statusFilter, setStatusFilter, dateRange, setDateRange, blockedFilter, setBlockedFilter, requiresApproval, setRequiresApproval, stuckFilter, setStuckFilter, stuckMinSeconds, setStuckMinSeconds, failureClassFilter, setFailureClassFilter } = useSearchFilters(taskTypeFilterValues);

  // useTaskModal runs after useSettings so it can consume the published
  // task-capability contract for its launchable-type guard.
  const {
    showNewTaskModal, setShowNewTaskModal,
    newTaskRepo, newTaskBranch, newTaskTitle, newTaskPrompt, newTaskType,
    newTaskPrNumber, newTaskPrBranch, newTaskModel, newTaskEffort,
    setNewTaskRepo, setNewTaskBranch, setNewTaskTitle, setNewTaskPrompt,
    setNewTaskType, setNewTaskPrNumber, setNewTaskPrBranch, setNewTaskModel, setNewTaskEffort,
    repoOptions, repoLoading, repoError, useManualRepo, setUseManualRepo,
    branchOptions, branchLoading, branchError, useManualBranch, setUseManualBranch, setBranchOptions,
    launchableTaskTypeOptions,
    openNewTaskModal, openDuplicateTaskModal, openPrefilledTaskModal, openWebResearchTaskModal, handleRepoChange, handleCreateTask, getDefaultModelForType, getDefaultEffortForType,
  } = useTaskModal(defaultTaskType, addToast, fetchTasksRef, defaultTaskBranchRef, selectedProjectRepoRef, taskCapabilityContract);

  const {
    allowedUsers, addAllowedUser, updateAllowedUserRole, removeAllowedUser,
  } = useUserManagement(showUserManagement);

  const {
    projectMembers, fetchProjectMembers, addProjectMember, updateProjectMemberRole, removeProjectMember,
  } = useProjectMembers();

  // ── Notification preferences ─────────────────────────────────────────────
  const {
    notifyOnComplete, setNotifyOnComplete,
    notifyOnFail, setNotifyOnFail,
    notifyOnApprovalNeeded, setNotifyOnApprovalNeeded,
    soundOnComplete, setSoundOnComplete,
    notifyOnHighSignalMessage, setNotifyOnHighSignalMessage,
    notifyRef, notifyOnFailRef, notifyOnApprovalNeededRef, soundOnCompleteRef,
  } = useNotificationPrefs();

  // ── Projects domain ──────────────────────────────────────────────────────
  const {
    projects, projectsFetchError,
    fetchProjects, handlePauseAllAutonomous, handleClearWaveDeps,
  } = useProjectsState(addToast, fetchTasksRef);

  // ── Alerts domain ────────────────────────────────────────────────────────
  const { alerts, fetchAlerts, dismissAlert, dismissAllAlerts } = useAlertsState(addToast, selectedProjectId);

  // ── Tasks domain ─────────────────────────────────────────────────────────
  const {
    tasks, totalTasks, globalActiveCount, globalEvaluatingCount, globalQueuedCount,
    initialLoading, refreshing,
    page, setPage, limit, setLimit, refreshInterval, setRefreshInterval,
    actionLoadingMap,
    lastRefresh, apiHealthy, lastFetchTime,
    needsAttentionCount,
    fetchTasks,
    handleApprove, handleRequestChanges, handleCancel, handleRetry,
    handleEditPrompt, handleDismiss, handleArchiveTask, handleUnarchiveTask,
    handleBulkArchive, handleBulkArchiveSelected, handleBulkArchiveColumn,
    handleBulkAction, handleBulkCancel,
  } = useTasksState({
    addToast, setError, statusFilter, addMetricsPoint,
    notifyRef, notifyOnFailRef, notifyOnApprovalNeededRef, soundOnCompleteRef,
    fetchArchiveTasks,
    selectedTaskIds, setSelectedTaskIds, setSelectionMode, setConfirmDialog,
    fetchTasksRef, prevTasksRef,
  });

  // ── Project-scoped config domain ─────────────────────────────────────────
  const {
    autoRemediationConfig, autoRemediationStats, handleUpdateAutoRemediationConfig,
    issueImplementationConfig, handleUpdateIssueImplementationConfig,
    autoReviewConfig, handleUpdateAutoReviewConfig,
    integrationBranchData, handleUpdateIntegrationBranch,
    promotionTargetData, handleUpdatePromotionTarget,
    taskBranchDefaultsReady, taskBranchDefaultsLoading, taskBranchDefaultsError,
  } = useProjectConfig(addToast, selectedProjectId, projects, selectedProjectRepoRef, defaultTaskBranchRef);

  // ── Last visit timestamp ──────────────────────────────────────────────────
  // Captures when the user previously loaded the app, scoped per operator.
  // Global key retained as migration source and unauthenticated fallback.
  const LAST_VISIT_GLOBAL_KEY = 'blueprint-admin:lastVisitTimestamp';
  const lastVisitKey = (email: string | null | undefined) =>
    email ? `blueprint-admin:lastVisitTimestamp:${email}` : LAST_VISIT_GLOBAL_KEY;

  const [lastVisitTimestamp, setLastVisitTimestamp] = useState<number | null>(null);
  const lastVisitInitRef = useRef(false);

  useEffect(() => {
    if (!user || lastVisitInitRef.current) return;
    lastVisitInitRef.current = true;
    try {
      const key = lastVisitKey(user.email);
      const stored = localStorage.getItem(key);
      let ts = stored ? parseInt(stored, 10) : null;
      // Migration: seed from global key when per-operator key is absent
      if ((!ts || isNaN(ts)) && key !== LAST_VISIT_GLOBAL_KEY) {
        const globalStored = localStorage.getItem(LAST_VISIT_GLOBAL_KEY);
        const globalTs = globalStored ? parseInt(globalStored, 10) : null;
        if (globalTs && !isNaN(globalTs)) ts = globalTs;
      }
      setLastVisitTimestamp(ts && !isNaN(ts) ? ts : null);
      localStorage.setItem(key, String(Date.now()));
    } catch { /* ignore */ }
  }, [user]);

  // Hydrate lastVisitTimestamp from backend (cross-device baseline via MessagesLastSeen store).
  // No project_id → returns max last-seen across all operator projects. Backend wins when newer.
  useEffect(() => {
    if (!user) return;
    authGet(`${API_BASE_URL}/api/admin/messages/last-seen`)
      .then(res => res.ok ? res.json() : null)
      .then((data: { last_seen_at?: number | null } | null) => {
        const backendTs = (data && typeof data.last_seen_at === 'number' && data.last_seen_at > 0)
          ? data.last_seen_at
          : null;
        if (backendTs !== null) {
          setLastVisitTimestamp(prev => (prev == null || backendTs > prev) ? backendTs : prev);
        }
      })
      .catch(() => {});
  }, [user]);

  // ── Research feed ──────────────────────────────────────────────────────────
  useEffect(() => {
    if (!showResearchBrowser || !selectedProjectId) { setResearchFeed([]); return; }
    const pid = selectedProjectId;
    let cancelled = false;
    apiFetch(
      `${API_BASE_URL}/api/admin/tasks?task_type=research&archived=false&limit=500&project_id=${encodeURIComponent(pid)}`,
    )
      .then(async res => {
        if (!res.ok || cancelled) return;
        const data = await res.json();
        const all: Task[] = Array.isArray(data) ? data : (data.tasks ?? []);
        if (!cancelled) setResearchFeed(all.filter(t => t.task_type === 'research' && Array.isArray(t.research_findings) && t.research_findings.length > 0));
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [showResearchBrowser, selectedProjectId]);

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

  // Leading fetches on mount / user change — kicks off the initial load.
  useEffect(() => {
    if (!user) return;
    fetchSettings();
    fetchProjects();
    fetchAlerts();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user]);

  // Coordinated polling on the shared, visibility-aware clock. Project list
  // refreshes every 60s; tasks+alerts refresh on the configured cadence
  // (disabled when refreshInterval === 0). Both pause when the tab is hidden
  // and catch up on return. Polling-only (the effect above owns the leading
  // load) so mount behavior is unchanged.
  usePoller(() => { fetchProjects(true); }, {
    intervalMs: 60_000,
    enabled: !!user,
    leading: false,
    deps: [user],
  });
  usePoller(() => { fetchTasksRef.current(); fetchAlerts(true); }, {
    intervalMs: refreshInterval,
    enabled: !!user && refreshInterval !== 0,
    leading: false,
    deps: [user, refreshInterval],
  });

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
        case '3':
          setViewMode('timeline');
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

  // ── Cross-domain glue handlers ────────────────────────────────────────────

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

  const handleToggleSettings = () => {
    setShowSettings(prev => {
      if (!prev) fetchSettings();
      return !prev;
    });
  };

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
    searchQuery, setSearchQuery, taskTypeFilter, setTaskTypeFilter, statusFilter, setStatusFilter, dateRange, setDateRange, blockedFilter, setBlockedFilter, requiresApproval, setRequiresApproval, stuckFilter, setStuckFilter, stuckMinSeconds, setStuckMinSeconds, failureClassFilter, setFailureClassFilter,
    // Projects
    projects, projectsFetchError, selectedProjectId, setSelectedProjectId, handlePauseAllAutonomous, fetchProjects,
    // Log viewer
    logTask, logLines, logsConnecting, wsDisconnected, wsDisconnectReason, handleViewLogs, closeLogViewer,
    // Research browser
    showResearchBrowser, setShowResearchBrowser,
    showKnowledgeBrowser, setShowKnowledgeBrowser,
    researchFeed,
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
    launchableTaskTypeOptions,
    taskBranchDefaultsReady, taskBranchDefaultsLoading, taskBranchDefaultsError,
    openNewTaskModal, openDuplicateTaskModal, openPrefilledTaskModal, openWebResearchTaskModal, handleRepoChange, handleCreateTask, getDefaultModelForType, getDefaultEffortForType,
    // Defaults
    defaultTaskType, setDefaultTaskType,
    notifyOnComplete, setNotifyOnComplete,
    notifyOnFail, setNotifyOnFail,
    notifyOnApprovalNeeded, setNotifyOnApprovalNeeded,
    soundOnComplete, setSoundOnComplete,
    notifyOnHighSignalMessage, setNotifyOnHighSignalMessage,
    // Settings
    showSettings, settingsRef, handleToggleSettings,
    maxConcurrentVMs, maxRetries, maxAutonomousTasksPerHour, usingMaxPlan,
    autonomousResearchIntervalMinutes, directionCooldownMinutes, strategicResearchFrequency,
    maxGlobalAutonomousTasksPerHour, maxPipelineSlots, reservedSlotsForTypes,
    evaluatingPipelineTimeoutMinutes, evaluatingFailHours, ciBackstopTimeoutMinutes,
    updateMaxGlobalAutonomousRate, updateMaxPipelineSlots, updateReservedSlotsForTypes,
    updateEvaluatingPipelineTimeout, updateEvaluatingFailHours, updateCiBackstopTimeout,
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
    integrationBranchData, handleUpdateIntegrationBranch,
    promotionTargetData, handleUpdatePromotionTarget,
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
    taskCapabilityContract,
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
    fetchDirectionsRef, strategistActive,
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
