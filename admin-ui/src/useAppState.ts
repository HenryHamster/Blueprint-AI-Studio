/* eslint-disable react-hooks/refs, react-hooks/set-state-in-effect */
import { useState, useEffect, useMemo, useRef, useCallback, type MutableRefObject } from 'react';
import type { Task } from './types/task';
import type { Project } from './types/project';
import type { ProjectMessage } from './types/messages';
import { API_BASE_URL, apiFetch, authHeaders, authGet } from './api/core';
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
import { normalizePrimaryTab, useUIState } from './hooks/useUIState';
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
import { isEditableShortcutEvent } from './hooks/keyboardTargets';
import { resolveAccess } from './accessControl';
import { clearGitHubOAuthCallbackQuery, clearNewProjectOAuthResume } from './utils/newProjectOAuthResume';

export type { DeployState } from './types/settings';

export function effectiveSelectedProjectId(
  persistedProjectId: string | null,
  projectsLoaded: boolean,
  projectsFetchError: string | null,
  canViewProject: boolean,
): string | null {
  return projectsLoaded && !projectsFetchError && canViewProject
    ? persistedProjectId
    : null;
}

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
interface UseAppStateOptions {
  refreshStrategicReadModelRef?: MutableRefObject<() => void>;
  refreshMessagesRef?: MutableRefObject<(message?: ProjectMessage, canonicalRefetchRequired?: boolean) => void>;
  refreshFleetRunnersRef?: MutableRefObject<() => void>;
}

function useShallowMemo<T extends Record<string, unknown>>(value: T): T {
  type StableCallbackSlot = {
    current: (...args: unknown[]) => unknown;
    stable: (...args: unknown[]) => unknown;
  };
  const previousRef = useRef<T | null>(null);
  const callbackSlotsRef = useRef(new Map<keyof T, StableCallbackSlot>());

  return useMemo(() => {
    // Composed domain hooks do not all memoize their callbacks. Give every
    // function-valued facade field a stable delegate that always invokes the
    // latest implementation; otherwise one fresh callback defeats memoization
    // of the entire return object (or, worse, omitting it from dependencies
    // leaves consumers with a stale closure).
    const normalized = {} as T;
    const normalizedRecord = normalized as Record<keyof T, unknown>;
    for (const key of Object.keys(value) as Array<keyof T>) {
      const field = value[key];
      if (typeof field !== 'function') {
        normalizedRecord[key] = field;
        continue;
      }
      let slot = callbackSlotsRef.current.get(key);
      if (!slot) {
        const newSlot = {} as StableCallbackSlot;
        newSlot.current = field as (...args: unknown[]) => unknown;
        newSlot.stable = (...args: unknown[]) => newSlot.current(...args);
        slot = newSlot;
        callbackSlotsRef.current.set(key, slot);
      } else {
        slot.current = field as (...args: unknown[]) => unknown;
      }
      normalizedRecord[key] = slot.stable;
    }

    const previous = previousRef.current;
    const keys = Object.keys(normalized) as Array<keyof T>;
    if (
      previous !== null
      && keys.length === Object.keys(previous).length
      && keys.every(key => Object.is(normalized[key], previous[key]))
    ) {
      return previous;
    }
    previousRef.current = normalized;
    return normalized;
  }, [value]);
}

export function useAppState({ refreshStrategicReadModelRef, refreshMessagesRef, refreshFleetRunnersRef }: UseAppStateOptions = {}) {
  // ── Composed hooks ──────────────────────────────────────────────────────
  const { user, authLoading, userRole, accessDenied, error, setError } = useAuth();
  const platformAccess = resolveAccess(userRole, undefined);
  const { toasts, addToast, dismissToast } = useToasts();
  const { viewMode, setViewMode, compactMode, setCompactMode, columnPrefs, setColumnPrefs, defaultTaskType, setDefaultTaskType } = useViewPreferences();
  const { metricsHistory, cumulativeCost, cumulativeVmHours, metricsWindow, setMetricsWindow, addMetricsPoint } = useMetrics();
  const {
    showArchive, setShowArchive, archiveTasks, archiveLoading,
    archivePage, setArchivePage, archivePageSize, setArchivePageSize,
    archiveStatusFilter, setArchiveStatusFilter,
    archiveSearchQuery, setArchiveSearchQuery,
    archiveDateRange, setArchiveDateRange,
    archiveTotalTasks, fetchArchiveTasks,
    archiveSortOrder, setArchiveSortOrder,
    handlePurgeArchive,
  } = useArchive(addToast);

  // ── Shared refs (cross-domain glue owned by the facade) ──────────────────
  const fetchTasksRef = useRef<() => Promise<void>>(async () => {});
  const fetchCountsRef = useRef<() => Promise<void>>(async () => {});
  const fetchAlertsRef = useRef<() => void>(() => {});
  // Populated by useDirectionsData (when the directions panel is mounted) so the
  // shared WS handler in useLogViewer can live-refresh the directions view.
  const fetchDirectionsRef = useRef<() => void>(() => {});
  const defaultTaskBranchRef = useRef<string | null>(null);
  const selectedProjectRepoRef = useRef<string | null>(null);
  const selectedProjectIdRef = useRef<string | null>(null);
  const prevTasksRef = useRef<Task[]>([]);

  // Moved before useDeploymentState so it can be passed as a parameter
  const [persistedSelectedProjectId, setSelectedProjectId] = usePersistedState<string | null>('selectedProject', null);

  // Project membership is the existing source of truth for project-scoped
  // Runtime reads. Keep it available before the deployment hook so global
  // admin health and project diagnostics can be gated independently.
  const {
    projects, setProjects, projectsFetchError, projectsLoaded,
    fetchProjects, handlePauseAllAutonomous, handleClearWaveDeps,
  } = useProjectsState(
    addToast,
    fetchTasksRef,
  );
  const selectedProjectAccess = resolveAccess(
    userRole,
    projects.find(project => project.id === persistedSelectedProjectId)?.my_role,
  );
  // A persisted selection is only safe to use as a request key after the
  // authoritative membership list has loaded successfully and still contains
  // it. Until then every project-scoped hook/poller receives null.
  const selectedProjectId = effectiveSelectedProjectId(
    persistedSelectedProjectId,
    projectsLoaded,
    projectsFetchError,
    selectedProjectAccess.canViewProject,
  );

  useEffect(() => {
    if (
      !projectsLoaded
      || projectsFetchError
      || !persistedSelectedProjectId
      || projects.some(project => project.id === persistedSelectedProjectId)
    ) return;
    setSelectedProjectId(null);
  }, [projects, projectsFetchError, projectsLoaded, persistedSelectedProjectId, setSelectedProjectId]);

  // Keep the task launcher on the same validated identity used by reads.
  useEffect(() => { selectedProjectIdRef.current = selectedProjectId; }, [selectedProjectId]);

  const {
    activeTab, setActiveTab,
    showResearchBrowser, setShowResearchBrowser,
    showKnowledgeBrowser, setShowKnowledgeBrowser,
    showActivityFeed, setShowActivityFeed, lastActivityFeedOpen, prevLastActivityFeedOpen,
    showFleetDetail, setShowFleetDetail,
    showDirections, setShowDirections,
    showMetrics, setShowMetrics,
    showUserManagement, setShowUserManagement,
    selectionMode, setSelectionMode, selectedTaskIds, setSelectedTaskIds,
    toggleSelectionMode, toggleTaskSelection, bulkColumnToggle, shiftSelectRange,
    confirmDialog, setConfirmDialog,
  } = useUIState();
  // Runtime data is expensive and irrelevant on the default Chat surface.
  // Once Runtime has been opened, keep its health data live for its header and
  // workspace affordances even while the operator visits another tab.
  const [runtimeTabVisited, setRuntimeTabVisited] = useState(() => normalizePrimaryTab(activeTab) === 'runtime');
  // Keep a ref tracking the current tab so pollers can read it without stale closures.
  const activeTabRef = useRef(activeTab);
  useEffect(() => {
    if (normalizePrimaryTab(activeTab) === 'runtime') setRuntimeTabVisited(true);
    activeTabRef.current = activeTab;
  }, [activeTab]);

  const {
    deployState, setDeployState,
    handlePausePipeline, handleResumePipeline, handleResetHealth,
    handleRollback, handleMarkStable, handleRepairState,
    failedRollback,
    zoneHealth, orchestratorHealth, detailedHealth, diagnostics,
  } = useDeploymentState(addToast, user, fetchTasksRef, selectedProjectId, runtimeTabVisited, {
    isPlatformAdmin: platformAccess.isPlatformAdmin,
    canViewProject: selectedProjectAccess.canViewProject,
  });

  const {
    logTask, logLines, logsConnecting, wsDisconnected, wsDisconnectReason,
    handleViewLogs, closeLogViewer,
    wsRef, wsConnected,
    pipelineThrottle,
    ceilingDeferredProjectIds,
    gatedTaskTypes,
    strategistActive,
    cycleProgress,
    lastCycleCompletion,
    strategicSessionTranscript,
    trackedTaskIds, trackedLogs, trackTask, untrackTask, untrackAll,
  } = useLogViewer({
    fetchTasksRef,
    setDeployState,
    fetchDirectionsRef,
    refreshStrategicReadModelRef,
    refreshMessagesRef,
    fetchAlertsRef,
    refreshFleetRunnersRef,
    selectedProjectId,
    realtimeWsEnabled: !authLoading && Boolean(user),
    canViewProject: selectedProjectAccess.canViewProject,
    isAdmin: platformAccess.isPlatformAdmin,
  });

  const [researchFeed, setResearchFeed] = useState<Task[]>([]);
  const [fleetRunnersRefreshSignal, setFleetRunnersRefreshSignal] = useState(0);
  if (refreshFleetRunnersRef) {
    refreshFleetRunnersRef.current = () => setFleetRunnersRefreshSignal(signal => signal + 1);
  }

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
    directionChurnWindowCycles, directionChurnNonMergeThreshold, directionChurnMinMergeRate,
    directionLifetimeNonMergeThreshold, directionLifetimeMinMergeRate,
    directionReviewRecommendationThreshold,
    updateDirectionChurnWindowCycles, updateDirectionChurnNonMergeThreshold,
    updateDirectionChurnMinMergeRate, updateDirectionLifetimeNonMergeThreshold,
    updateDirectionLifetimeMinMergeRate, updateDirectionReviewRecommendationThreshold,
    providerTierModelMap, taskTypeModelPreferences,
    taskTypeCategories, categoryRouting, taskTypeRouting,
    providerSettingsByEffort,
    providerSelectionMode,
    crossProviderFallbackEnabled, providers,
    taskCapabilityContract,
    providerSettingsDefaults, taskTypeProviderOverrides,
    buildConfig, setBuildConfig,
    fetchSettings,
    fetchMaxPlanAccounts,
    addMaxPlanAccount, removeMaxPlanAccount, toggleMaxPlanAccount, resetAccountRateLimits,
    refreshAccountModels,
    updateAccountCaps, updateMaxPlanAccount,
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
  const { searchQuery, setSearchQuery, taskTypeFilter, setTaskTypeFilter, statusFilter, setStatusFilter, dateRange, setDateRange, blockedFilter, setBlockedFilter, requiresApproval, setRequiresApproval, stuckFilter, setStuckFilter, stuckMinSeconds, setStuckMinSeconds, failureClassFilter, setFailureClassFilter, directionFilter, setDirectionFilter } = useSearchFilters(taskTypeFilterValues);

  // useTaskModal runs after useSettings so it can consume the published
  // task-capability contract for its launchable-type guard.
  const {
    showNewTaskModal, setShowNewTaskModal,
    newTaskRepo, newTaskBranch, newTaskTitle, newTaskPrompt, newTaskType,
    newTaskPrNumber, newTaskPrBranch, newTaskModel, newTaskEffort,
    setNewTaskRepo, setNewTaskBranch, setNewTaskTitle, setNewTaskPrompt,
    setNewTaskType, setNewTaskPrNumber, setNewTaskPrBranch, setNewTaskModel, setNewTaskEffort,
    computeExperiment, setComputeExperiment,
    ideaText, setIdeaText, draftNotes, setDraftNotes, onDraftApplied,
    preflightResult, preflightLoading, preflightError,
    repoOptions, repoLoading, repoError, useManualRepo, setUseManualRepo,
    branchOptions, branchLoading, branchError, useManualBranch, setUseManualBranch, setBranchOptions,
    launchableTaskTypeOptions,
    fleetRunners, fleetRunnersLoading,
    openNewTaskModal, openDuplicateTaskModal, openPrefilledTaskModal, openWebResearchTaskModal, handleRepoChange, handleCreateTask, getDefaultModelForType, getDefaultEffortForType,
  } = useTaskModal(defaultTaskType, addToast, fetchTasksRef, defaultTaskBranchRef, selectedProjectRepoRef, taskCapabilityContract, selectedProjectIdRef);

  const {
    allowedUsers, addAllowedUser, updateAllowedUserRole, removeAllowedUser,
  } = useUserManagement(showUserManagement);

  const {
    projectMembers, projectMembersProjectId, activateProjectMembers, fetchProjectMembers, addProjectMember, updateProjectMemberRole, removeProjectMember,
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
  // ── Alerts domain ────────────────────────────────────────────────────────
  const { alerts, fetchAlerts, dismissAlert, dismissAllAlerts } = useAlertsState(addToast, selectedProjectId);
  fetchAlertsRef.current = fetchAlerts;

  // ── Tasks domain ─────────────────────────────────────────────────────────
  const {
    tasks, totalTasks, globalActiveCount, globalEvaluatingCount, globalQueuedCount,
    scopedActiveCount, scopedEvaluatingCount, scopedQueuedCount,
    initialLoading, refreshing, fetchEpoch,
    page, setPage, limit, setLimit, refreshInterval, setRefreshInterval,
    actionLoadingMap,
    lastRefresh, apiHealthy, lastFetchTime,
    needsAttentionCount,
    fetchTasks, fetchCounts,
    handleApprove, handleRequestChanges, handleCancel, handleRetry,
    handleEditPrompt, handleDismiss, handleArchiveTask, handleUnarchiveTask,
    handleBulkArchive, handleBulkArchiveSelected, handleBulkArchiveColumn,
    handleBulkAction, handleBulkCancel,
    handleResetIterationCap, handleTriggerRollup,
  } = useTasksState({
    addToast, setError, statusFilter, addMetricsPoint,
    notifyRef, notifyOnFailRef, notifyOnApprovalNeededRef, soundOnCompleteRef,
    fetchArchiveTasks,
    selectedTaskIds, setSelectedTaskIds, setSelectionMode, setConfirmDialog,
    fetchTasksRef, prevTasksRef,
    selectedProjectId, canOperateProject: selectedProjectAccess.canOperateProject, directionFilter,
  });
  fetchCountsRef.current = fetchCounts;

  // Project-scoped callbacks can outlive the surface that created them (for
  // example a bulk confirmation opened immediately before switching projects).
  // Tear down those closures whenever scope or operate capability changes.
  const projectTransientScopeRef = useRef({
    projectId: selectedProjectId,
    canOperate: selectedProjectAccess.canOperateProject,
  });
  useEffect(() => {
    const previous = projectTransientScopeRef.current;
    const canOperate = selectedProjectAccess.canOperateProject;
    if (previous.projectId === selectedProjectId && previous.canOperate === canOperate) return;
    projectTransientScopeRef.current = { projectId: selectedProjectId, canOperate };
    setSelectedTaskIds(new Set());
    setSelectionMode(false);
    setConfirmDialog(null);
    closeLogViewer();
    setShowNewTaskModal(false);
  }, [
    selectedProjectId,
    selectedProjectAccess.canOperateProject,
    closeLogViewer,
    setConfirmDialog,
    setSelectedTaskIds,
    setSelectionMode,
    setShowNewTaskModal,
  ]);

  // ── Project-scoped config domain ─────────────────────────────────────────
  const {
    autoRemediationConfig, autoRemediationStats, handleUpdateAutoRemediationConfig,
    issueImplementationConfig, handleUpdateIssueImplementationConfig,
    autoReviewConfig, handleUpdateAutoReviewConfig,
    resourceEnvelopeData, resourceEnvelopeLoading, resourceEnvelopeError, handleUpdateResourceEnvelope,
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

  useEffect(() => {
    if (!user || !selectedProjectId) return;
    const selectedProject = projects.find(project => project.id === selectedProjectId);
    if (!selectedProject) return;
    if (selectedProject.capability_proof_summary && selectedProject.capability_adoption_verdicts) return;

    let cancelled = false;
    authGet(`${API_BASE_URL}/api/admin/projects/${encodeURIComponent(selectedProjectId)}/summary`)
      .then(async res => {
        if (!res.ok || cancelled) return;
        const detailedProject = await res.json() as Project;
        if (cancelled) return;
        setProjects(prev => prev.map(project => (
          project.id === selectedProjectId ? { ...project, ...detailedProject } : project
        )));
      })
      .catch(() => {});

    return () => { cancelled = true; };
  }, [user, selectedProjectId, projects, setProjects]);

  // Handle OAuth redirect query params
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const connected = params.get('github_connected');
    const ghError = params.get('github_error');
    if (connected) {
      setError(null);
      setGithubAccount(connected);
      clearGitHubOAuthCallbackQuery();
    } else if (ghError) {
      const messages: Record<string, string> = {
        state_expired: 'GitHub OAuth expired — try again',
        invalid_state: 'GitHub OAuth failed — invalid state',
        no_installations: 'No GitHub App installations found — install the app on your repos first',
        failed: 'GitHub connection failed — try again',
      };
      setError(messages[ghError] || `GitHub error: ${ghError}`);
      clearNewProjectOAuthResume();
      clearGitHubOAuthCallbackQuery();
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
    if (!user || userRole === null) return;
    if (platformAccess.canReadPlatformSettings) {
      fetchSettings();
      fetchMaxPlanAccounts();
    }
    fetchProjects();
    fetchAlerts();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user, userRole]);

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
  usePoller(() => {
    // On the pipeline tab, run the full-row poll so the task list stays current.
    // On other tabs, the header badge only needs aggregate counts — use the
    // cheap /tasks/counts endpoint to avoid loading 50-200 full task rows.
    if (normalizePrimaryTab(activeTabRef.current) === 'pipeline') {
      fetchTasksRef.current();
    } else {
      fetchCountsRef.current();
    }
    fetchAlerts(true);
  }, {
    intervalMs: refreshInterval,
    enabled: !!user && refreshInterval !== 0,
    leading: false,
    deps: [user, refreshInterval],
  });

  // Keyboard shortcuts
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return;

      if (e.key === 'Escape') {
        if (showNewTaskModal) { setShowNewTaskModal(false); return; }
        if (logTask) { closeLogViewer(); return; }
        if (selectionMode) {
          if (selectedTaskIds.size > 0) { setSelectedTaskIds(new Set()); } else { toggleSelectionMode(); }
          return;
        }
        if (showResearchBrowser) { setShowResearchBrowser(false); return; }
        if (showActivityFeed) { setShowActivityFeed(false); return; }
        if (showArchive) { setShowArchive(false); return; }
        return;
      }

      if (isEditableShortcutEvent(e)) return;

      switch (e.key) {
        case 'r':
          if (selectedProjectAccess.canOperateProject && selectionMode && selectedTaskIds.size > 0) {
            e.preventDefault();
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
          }
          break;
        case 'Delete':
          if (selectedProjectAccess.canOperateProject && selectionMode && selectedTaskIds.size > 0) {
            e.preventDefault();
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
            e.preventDefault();
            const ids = Array.from(selectedTaskIds);
            const taskId = ids[ids.length - 1];
            const task = tasks.find(t => t.id === taskId);
            if (task) { handleViewLogs(task); }
          }
          break;
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showNewTaskModal, logTask, showResearchBrowser, showActivityFeed, showArchive, selectionMode, selectedTaskIds, tasks, selectedProjectAccess.canOperateProject]);

  // ── Cross-domain glue handlers ────────────────────────────────────────────

  const handleRestartOrchestrator = useCallback(async () => {
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
  }, [addToast]);

  const refreshSettings = useCallback(() => {
    if (!platformAccess.canReadPlatformSettings) return;
    fetchSettings();
    // Refresh the authoritative roster every time settings are entered so a
    // just-added/edited account is always present without waiting on the heavy
    // settings payload.
    fetchMaxPlanAccounts();
  }, [platformAccess.canReadPlatformSettings, fetchSettings, fetchMaxPlanAccounts]);

  const handleUpdateBuildConfig = useCallback(async (config: import('./types/project').BuildConfig) => {
    if (!selectedProjectId) return;
    try {
      await updateBuildConfig(selectedProjectId, config);
      addToast('Build config saved', 'success');
      fetchProjects(true);
    } catch {
      addToast('Failed to save build config', 'error');
    }
  }, [selectedProjectId, updateBuildConfig, addToast, fetchProjects]);

  const handleDetectBuildConfig = useCallback(async () => {
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
  }, [selectedProjectId, addToast, fetchProjects, setBuildConfig]);

  return useShallowMemo({
    // Auth
    user, authLoading, userRole, selectedProjectAccess, accessDenied, error, setError,
    // Tasks
    tasks, totalTasks, globalActiveCount, globalEvaluatingCount, globalQueuedCount,
    scopedActiveCount, scopedEvaluatingCount, scopedQueuedCount,
    initialLoading, refreshing, fetchEpoch, page, setPage, limit, setLimit, refreshInterval, setRefreshInterval,
    needsAttentionCount,
    actionLoadingMap,
    fetchTasks, handleApprove, handleClearWaveDeps, handleRequestChanges, handleCancel, handleRetry, handleEditPrompt, handleDismiss, handleArchiveTask, handleUnarchiveTask,
    handleBulkArchive, handleBulkArchiveSelected, handleBulkArchiveColumn, handleBulkAction, handleBulkCancel,
    handleResetIterationCap, handleTriggerRollup,
    selectionMode, setSelectionMode, selectedTaskIds, toggleSelectionMode, toggleTaskSelection, bulkColumnToggle, shiftSelectRange,
    confirmDialog, setConfirmDialog,
    // Toasts
    toasts, addToast, dismissToast,
    // Alerts
    alerts, dismissAlert, dismissAllAlerts,
    // View
    viewMode, setViewMode,
    compactMode, setCompactMode,
    // Column preferences
    columnPrefs, setColumnPrefs,
    // Search/filter
    searchQuery, setSearchQuery, taskTypeFilter, setTaskTypeFilter, statusFilter, setStatusFilter, dateRange, setDateRange, blockedFilter, setBlockedFilter, requiresApproval, setRequiresApproval, stuckFilter, setStuckFilter, stuckMinSeconds, setStuckMinSeconds, failureClassFilter, setFailureClassFilter, directionFilter, setDirectionFilter,
    // Projects
    projects, projectsFetchError, selectedProjectId, setSelectedProjectId, handlePauseAllAutonomous, fetchProjects, projectsLoaded,
    // Log viewer
    logTask, logLines, logsConnecting, wsDisconnected, wsDisconnectReason, handleViewLogs, closeLogViewer, strategicSessionTranscript,
    // Research browser
    showResearchBrowser, setShowResearchBrowser,
    showKnowledgeBrowser, setShowKnowledgeBrowser,
    researchFeed,
    // Activity feed
    showActivityFeed, setShowActivityFeed, lastActivityFeedOpen, prevLastActivityFeedOpen,
    // Messages
    showFleetDetail, setShowFleetDetail,
    // New task modal
    showNewTaskModal, setShowNewTaskModal,
    newTaskRepo, newTaskBranch, newTaskTitle, newTaskPrompt, newTaskType,
    newTaskPrNumber, newTaskPrBranch, newTaskModel, newTaskEffort,
    setNewTaskRepo, setNewTaskBranch, setNewTaskTitle, setNewTaskPrompt,
    setNewTaskType, setNewTaskPrNumber, setNewTaskPrBranch, setNewTaskModel, setNewTaskEffort,
    computeExperiment, setComputeExperiment,
    ideaText, setIdeaText, draftNotes, setDraftNotes, onDraftApplied,
    preflightResult, preflightLoading, preflightError,
    repoOptions, repoLoading, repoError, useManualRepo, setUseManualRepo,
    branchOptions, branchLoading, branchError, useManualBranch, setUseManualBranch, setBranchOptions,
    launchableTaskTypeOptions,
    fleetRunners, fleetRunnersLoading,
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
    refreshSettings,
    maxConcurrentVMs, maxRetries, maxAutonomousTasksPerHour, usingMaxPlan,
    autonomousResearchIntervalMinutes, directionCooldownMinutes, strategicResearchFrequency,
    maxGlobalAutonomousTasksPerHour, maxPipelineSlots, reservedSlotsForTypes,
    evaluatingPipelineTimeoutMinutes, evaluatingFailHours, ciBackstopTimeoutMinutes,
    updateMaxGlobalAutonomousRate, updateMaxPipelineSlots, updateReservedSlotsForTypes,
    updateEvaluatingPipelineTimeout, updateEvaluatingFailHours, updateCiBackstopTimeout,
    directionChurnWindowCycles, directionChurnNonMergeThreshold, directionChurnMinMergeRate,
    directionLifetimeNonMergeThreshold, directionLifetimeMinMergeRate,
    directionReviewRecommendationThreshold,
    updateDirectionChurnWindowCycles, updateDirectionChurnNonMergeThreshold,
    updateDirectionChurnMinMergeRate, updateDirectionLifetimeNonMergeThreshold,
    updateDirectionLifetimeMinMergeRate, updateDirectionReviewRecommendationThreshold,
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
    resourceEnvelopeData, resourceEnvelopeLoading, resourceEnvelopeError, handleUpdateResourceEnvelope,
    integrationBranchData, handleUpdateIntegrationBranch,
    promotionTargetData, handleUpdatePromotionTarget,
    // Max plan accounts
    maxPlanAccounts, newAccountName, setNewAccountName, newAccountProvider, setNewAccountProvider,
    newAccountAuthMode, setNewAccountAuthMode, newAccountToken, setNewAccountToken,
    newAccountProviderMetadata, setNewAccountProviderMetadata, newAccountClaudeConfigJson, setNewAccountClaudeConfigJson,
    newAccountDailyTaskCap, setNewAccountDailyTaskCap,
    newAccountMaxConcurrentTasks, setNewAccountMaxConcurrentTasks,
    newAccountAutoDisableAfterRateLimits, setNewAccountAutoDisableAfterRateLimits,
    fetchMaxPlanAccounts, addMaxPlanAccount, removeMaxPlanAccount, toggleMaxPlanAccount, resetAccountRateLimits, refreshAccountModels, updateAccountCaps, updateMaxPlanAccount,
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
    fleetRunnersRefreshSignal,
    // GitHub
    githubAccount, connectGitHub,
    // Primary tab navigation
    activeTab, setActiveTab,
    // Directions
    showDirections, setShowDirections,
    fetchDirectionsRef, strategistActive, cycleProgress, lastCycleCompletion,
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
    handlePurgeArchive,
    // Misc
    lastVisitTimestamp,
    lastRefresh, wsRef,
    // Status bar
    apiHealthy, wsConnected, lastFetchTime,
    // Task tracking
    trackedTaskIds, trackedLogs, trackTask, untrackTask, untrackAll,
    // Deploy state
    deployState, handlePausePipeline, handleResumePipeline, handleResetHealth,
    handleRollback, handleMarkStable, handleRepairState,
    failedRollback,
    // Zone health
    zoneHealth,
    // Orchestrator health
    orchestratorHealth,
    detailedHealth,
    // Diagnostics
    diagnostics,
    pipelineThrottle,
    ceilingDeferredProjectIds,
    gatedTaskTypes,
    // User management
    allowedUsers, showUserManagement, setShowUserManagement,
    addAllowedUser, updateAllowedUserRole, removeAllowedUser,
    // Project members
    projectMembers, projectMembersProjectId, activateProjectMembers, fetchProjectMembers, addProjectMember, updateProjectMemberRole, removeProjectMember,
  });
}
