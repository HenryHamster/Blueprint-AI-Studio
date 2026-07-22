/* eslint-disable react-hooks/refs, react-hooks/set-state-in-effect */
import { useMemo, useRef, useEffect, useState, useCallback, lazy, Suspense } from 'react';
import { ErrorBoundary } from './components/ErrorBoundary';
import { Skeleton } from './components/DashboardMetricCardsPrimitives';
import { Activity, AlertCircle, CheckSquare, LayoutList, PanelRightClose, PanelRightOpen } from 'lucide-react';
import { ColumnsPicker } from './components/ColumnsPicker';
import type { ColumnPref } from './components/ColumnsPicker';
import { ConfirmDialog } from './components/ConfirmDialog';
import { useAppState } from './useAppState';
import { matchesTaskTypeFilter, resolveTaskCapabilities, TaskCapabilitiesProvider } from './taskCapabilities';
import { usePoller, DEFAULT_POLL_MS } from './hooks/usePoller';
const ResearchBrowser = lazy(() => import('./components/ResearchBrowser').then(m => ({ default: m.ResearchBrowser })));
const KnowledgeBrowser = lazy(() => import('./components/KnowledgeBrowser').then(m => ({ default: m.KnowledgeBrowser })));
const NewTaskModal = lazy(() => import('./components/NewTaskModal').then(m => ({ default: m.NewTaskModal })));
const NewProjectModal = lazy(() => import('./components/NewProjectModal').then(m => ({ default: m.NewProjectModal })));
import {
  addAccount,
  switchAccount,
  signInWithGoogle,
  signOut,
  signOutAll,
  getAccounts,
  getActiveAccountId,
} from './accountManager';
import { fetchTask } from './api/tasks';
import { apiFetch, API_BASE_URL } from './api/core';
import { loadLastSeen, loadEventPrefs, loadTemporaryMutes, fetchServerNoisePrefs, fetchServerTempMutes, resolveNoisePrefs, resolveServerTempMutes, type EventPref, type TemporaryMutes } from './components/MessagesPanel.utils';
const TaskDetailDrawer = lazy(() => import('./components/TaskDetailDrawer').then(m => ({ default: m.TaskDetailDrawer })));
import { ToastContainer } from './components/Toasts';
const FleetDetailDrawer = lazy(() => import('./components/FleetDetailDrawer').then(m => ({ default: m.FleetDetailDrawer })));
const FleetRunnersDashboard = lazy(() => import('./components/FleetRunnersDashboard').then(m => ({ default: m.FleetRunnersDashboard })));
import { HeaderBar, PrimaryTabNav } from './components/HeaderBar';
import { SearchFilterBar, SingleSelectDropdown } from './components/SearchFilterBar';
import { useSettingsRouterProps } from './hooks/useSettingsRouterProps';
import { normalizePrimaryTab, resolvePrimaryTabTarget, type PrimaryTab } from './hooks/useUIState';
import { defaultComputeExperimentForm } from './hooks/useTaskModal';
import type { SortBy } from './components/KanbanBoard';
import { ACTIVE_PIPELINE_STATUSES, CANCELLABLE_STATUSES, COLUMNS, SORT_KEY, TERMINAL_STATUSES } from './appWorkbenchConstants';
import { Pagination } from './components/Pagination';
const UserManagement = lazy(() => import('./components/UserManagement').then(m => ({ default: m.UserManagement })));
const ProjectMembers = lazy(() => import('./components/ProjectMembers').then(m => ({ default: m.ProjectMembers })));
const MessagesPanel = lazy(() => import('./components/MessagesPanel').then(m => ({ default: m.MessagesPanel })));
const ActivityFeed = lazy(() => import('./components/ActivityFeed'));
const StrategicContextWorkspace = lazy(() => import('./components/StrategicContextWorkspace').then(m => ({ default: m.StrategicContextWorkspace })));
const StrategicContextRail = lazy(() => import('./components/StrategicContextRail').then(m => ({ default: m.StrategicContextRail })));
const WelcomeScreen = lazy(() => import('./components/WelcomeScreen').then(m => ({ default: m.WelcomeScreen })));
const ProjectManagementModal = lazy(() => import('./components/ProjectManagementModal').then(m => ({ default: m.ProjectManagementModal })));
const BulkActionBar = lazy(() => import('./components/BulkActionBar').then(m => ({ default: m.BulkActionBar })));
const CommandPaletteHost = lazy(() => import('./components/CommandPaletteHost').then(m => ({ default: m.CommandPaletteHost })));
const KeyboardShortcutsOverlay = lazy(() => import('./components/KeyboardShortcutsOverlay').then(m => ({ default: m.KeyboardShortcutsOverlay })));
const NullBranchDiagnosticsPanel = lazy(() => import('./components/NullBranchDiagnosticsPanel').then(m => ({ default: m.NullBranchDiagnosticsPanel })));
import { FAILURE_CLASS_CONFIG } from './components/TaskCard';
import type { Task } from './components/TaskCard';
import type { ProjectMessage } from './types/messages';
import { VersionMismatchBanner } from './components/VersionMismatchBanner';
import type { ProjectManageSection, ProjectSection, RuntimeSection, RuntimeSettingsSection } from './components/OperatorWorkspaces';
const ProjectWorkspace = lazy(() => import('./components/OperatorWorkspaces').then(m => ({ default: m.ProjectWorkspace })));
const RuntimeWorkspace = lazy(() => import('./components/RuntimeWorkspace').then(m => ({ default: m.RuntimeWorkspace })));
const PipelineWorkspace = lazy(() => import('./components/PipelineWorkspace').then(m => ({ default: m.PipelineWorkspace })));
import { WorkbenchFrame, WorkbenchSplit, WorkbenchStatusBar, WorkbenchSurface } from './components/WorkbenchLayout';
import { useStrategicReadModel } from './hooks/useStrategicReadModel';
import { useStrategicLiveness } from './hooks/useStrategicLiveness';
import { useStrategicSession } from './hooks/useStrategicSession';
import { StrategicSessionControl, type StrategicInteractionMode } from './components/StrategicSessionControl';
import { useOperatorKeyboardShortcuts } from './hooks/useOperatorKeyboardShortcuts';
import { useStrategicMessageTarget } from './hooks/useStrategicMessageTarget';
import { useCommandPaletteState } from './hooks/useCommandPaletteState';
import { useStrategicContextNavigation } from './hooks/useStrategicContextNavigation';
import { useWorkbenchCommands, type PipelineSection } from './hooks/useWorkbenchCommands';
import { useVersionLifecycle, hasVersionLifecycleData } from './hooks/useVersionLifecycle';
import { type StrategicAccountSpendStatus, type StrategicStateAttentionItem } from './strategicStateApi';
import { parseTimestamp } from './utils/time';
import { matchesStatusFilter } from './utils/statusFilter';
import { pipelineTaskIdForAttentionItem } from './utils/strategicAttention';
import { STATUS_OPTIONS } from './constants';
import { reportUiLoadTiming } from './uiLoadTiming';

type PipelineDirectionOption = { id: string; text: string };
const PIPELINE_SECTION_ORDER: readonly PipelineSection[] = ['flow', 'board', 'archive'];
const PROJECT_SECTION_ORDER: readonly ProjectSection[] = ['running-update', 'directions', 'problem-model', 'design-doc', 'manage'];
const RUNTIME_SECTION_ORDER: readonly RuntimeSection[] = ['health', 'devices', 'compute', 'accounts', 'settings', 'analytics', 'version-lifecycle'];
const ACTIVE_TASK_STATUS_FILTER = STATUS_OPTIONS.map(option => option.value);

function cycleSection<T extends string>(sections: readonly T[], current: T, direction: -1 | 1): T {
  if (sections.length === 0) return current;
  const currentIndex = Math.max(0, sections.indexOf(current));
  const nextIndex = (currentIndex + direction + sections.length) % sections.length;
  return sections[nextIndex] ?? current;
}

function sameStringSet(a: readonly string[], b: readonly string[]): boolean {
  if (a.length !== b.length) return false;
  const values = new Set(b);
  return a.every(value => values.has(value));
}

function WorkspaceSkeleton() {
  return (
    <div className="flex-1 p-6 space-y-4">
      <Skeleton className="h-7 w-1/4" />
      <Skeleton className="h-4 w-1/2" />
      <Skeleton className="h-48 w-full rounded-lg" />
      <div className="space-y-3">
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-5/6" />
        <Skeleton className="h-4 w-2/3" />
      </div>
    </div>
  );
}

function BoardDataReadyReporter({ activeTab, initialLoading, refreshing, fetchEpoch }: { activeTab: PrimaryTab; initialLoading: boolean; refreshing: boolean; fetchEpoch: number }) {
  const navigationStartRef = useRef(0);
  const reportedRef = useRef(false);
  const targetEpochRef = useRef<number | null>(null);

  // On tab switch away from 'chat', record the start time and the epoch
  // we expect the next fetch to produce (current + 1). If a fetch completes
  // after the switch, the downstream effect captures it with real timing.
  useEffect(() => {
    if (activeTab === 'chat') return;
    navigationStartRef.current = performance.now();
    reportedRef.current = false;
    targetEpochRef.current = fetchEpoch + 1;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab]);

  // Report when a task-fetch completes after the switch (epoch advances past
  // target).  Otherwise fall back to rAF when nothing is currently loading.
  useEffect(() => {
    if (activeTab === 'chat' || reportedRef.current) return;

    // Epoch advanced → a fetch completed since the tab switch → report.
    if (targetEpochRef.current !== null && fetchEpoch >= targetEpochRef.current) {
      reportedRef.current = true;
      reportUiLoadTiming(activeTab, Math.round(performance.now() - navigationStartRef.current), 'board_data_ready');
      return;
    }

    // Nothing loading → fall back to rAF (paint boundary).
    if (!initialLoading && !refreshing) {
      const frame = requestAnimationFrame(() => {
        if (reportedRef.current) return;
        reportedRef.current = true;
        reportUiLoadTiming(activeTab, Math.round(performance.now() - navigationStartRef.current), 'board_data_ready');
      });
      return () => cancelAnimationFrame(frame);
    }
  }, [activeTab, fetchEpoch, initialLoading, refreshing]);

  return null;
}

export default function App() {
  const refreshStrategicReadModelRef = useRef<() => void>(() => {});
  const refreshMessagesRef = useRef<(message?: ProjectMessage, canonicalRefetchRequired?: boolean) => void>(() => {});
  const refreshFleetRunnersRef = useRef<() => void>(() => {});
  const [messageRealtimeRefreshSignal, setMessageRealtimeRefreshSignal] = useState(0);
  const [realtimeCanonicalProjection, setRealtimeCanonicalProjection] = useState<{
    message: ProjectMessage;
    canonicalRefetchRequired: boolean;
  } | null>(null);
  refreshMessagesRef.current = (message, canonicalRefetchRequired = false) => {
    if (message) setRealtimeCanonicalProjection({ message, canonicalRefetchRequired });
    else setMessageRealtimeRefreshSignal(value => value + 1);
  };
  const s = useAppState({ refreshStrategicReadModelRef, refreshMessagesRef, refreshFleetRunnersRef });
  const searchInputRef = useRef<HTMLInputElement>(null);
  const [pipelineContextTask, setPipelineContextTask] = useState<Task | null>(null);
  const pipelineContextRequestRef = useRef(0);
  // Dedup guard: track the last lastCycleCompletion object we toasted for so a
  // WS reconnect that re-emits the same event doesn't fire a second toast.
  const lastCycleCompletionToastRef = useRef<typeof s.lastCycleCompletion>(null);
  const { lastCycleCompletion: cycleCompletion, addToast, handleViewLogs: openTaskDetailDrawer } = s;
  const handleOpenTaskDetails = useCallback((task: Task) => {
    pipelineContextRequestRef.current += 1;
    setPipelineContextTask(task);
    openTaskDetailDrawer(task);
  }, [openTaskDetailDrawer]);
  useEffect(() => {
    if (!cycleCompletion) return;
    if (cycleCompletion === lastCycleCompletionToastRef.current) return;
    lastCycleCompletionToastRef.current = cycleCompletion;
    addToast('Strategic cycle complete', 'success');
  }, [cycleCompletion, addToast]);
  const [openMessageCount, setOpenMessageCount] = useState(0);
  const [newSinceLastVisit, setNewSinceLastVisit] = useState(0);
  const [activeAlerts, setActiveAlerts] = useState(0);
  const handleMessageCountChange = useCallback((count: number) => setOpenMessageCount(count), []);

  // Noise prefs for the closed-panel badge — loaded from localStorage on project change and
  // re-read when the panel closes so changes made inside the panel are reflected immediately.
  const closedPanelNoisePrefsRef = useRef<Record<string, EventPref>>({});
  const closedPanelTempMutesRef = useRef<TemporaryMutes>({});
  const [sortBy, setSortBy] = useState<SortBy>(() => {
    try {
      const stored = localStorage.getItem(SORT_KEY);
      return (stored as SortBy) ?? 'newest';
    } catch { return 'newest'; }
  });
  const { setFailureClassFilter, setRequiresApproval, setStatusFilter, setStuckFilter, setStuckMinSeconds } = s;
  useEffect(() => {
    const onFailureClassFilter = (e: Event) => {
      const failureClass = (e as CustomEvent<string>).detail;
      if (failureClass && failureClass in FAILURE_CLASS_CONFIG) setFailureClassFilter([failureClass]);
    };
    window.addEventListener('blueprint:failure-class-filter', onFailureClassFilter);
    return () => window.removeEventListener('blueprint:failure-class-filter', onFailureClassFilter);
  }, [setFailureClassFilter]);
  useEffect(() => {
    if (!sameStringSet(s.statusFilter, ACTIVE_TASK_STATUS_FILTER)) {
      setStatusFilter([...ACTIVE_TASK_STATUS_FILTER]);
    }
    if (s.requiresApproval) setRequiresApproval(false);
    if (s.stuckFilter) setStuckFilter(false);
    if (s.stuckMinSeconds !== 0) setStuckMinSeconds(0);
  }, [
    s.requiresApproval,
    s.statusFilter,
    s.stuckFilter,
    s.stuckMinSeconds,
    setRequiresApproval,
    setStatusFilter,
    setStuckFilter,
    setStuckMinSeconds,
  ]);
  useEffect(() => {
    localStorage.setItem(SORT_KEY, sortBy);
  }, [sortBy]);
  const resolvedTaskCapabilities = useMemo(
    () => resolveTaskCapabilities(s.taskCapabilityContract),
    [s.taskCapabilityContract],
  );
  const filterTaskTypeIds = resolvedTaskCapabilities.filterIds;
  useEffect(() => {
    if (openMessageCount > 0) {
      document.title = `(${openMessageCount}) Blueprint AI Studio`;
    } else {
      document.title = 'Blueprint AI Studio';
    }
  }, [openMessageCount]);
  const selectedProjectId = s.selectedProjectId;
  const rawActiveTab = s.activeTab;
  const activeTab: PrimaryTab = normalizePrimaryTab(rawActiveTab);
  const [visitedTabs, setVisitedTabs] = useState<ReadonlySet<PrimaryTab>>(() => new Set([activeTab]));
  useEffect(() => {
    setVisitedTabs(previous => previous.has(activeTab) ? previous : new Set([...previous, activeTab]));
  }, [activeTab]);
  const chatPollersEnabled = visitedTabs.has('chat');
  const runtimePollersEnabled = visitedTabs.has('runtime');
  const messagesPanelOpen = activeTab === 'chat';
  const [directions, setDirections] = useState<PipelineDirectionOption[]>([]);
  useEffect(() => {
    setDirections([]);
  }, [selectedProjectId]);
  usePoller(async (signal) => {
    if (!selectedProjectId) {
      setDirections([]);
      return;
    }
    const pid = selectedProjectId;
    try {
      const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(pid)}/directions?lite=1`, undefined, 2, 30000, signal);
      if (signal.aborted) return;
      if (!res.ok) {
        return;
      }
      const data: unknown = await res.json();
      if (signal.aborted || pid !== selectedProjectId) return;
      const list = Array.isArray(data) ? data : ((data as Record<string, unknown>)?.directions ?? []);
      setDirections((list as Array<Record<string, unknown>>)
        .map((d): PipelineDirectionOption | null => {
          const id = typeof d.id === 'string' ? d.id : '';
          if (!id) return null;
          const text = typeof d.text === 'string'
            ? d.text
            : typeof d.title === 'string'
              ? d.title
              : id;
          return { id, text };
        })
        .filter((d): d is PipelineDirectionOption => d !== null));
    } catch { /* ignore; the pipeline filter can operate without direction labels */ }
  }, {
    intervalMs: DEFAULT_POLL_MS,
    enabled: activeTab === 'pipeline' || activeTab === 'chat',
    deps: [selectedProjectId, activeTab],
  });
  // Clear message count when panel opens
  useEffect(() => {
    if (messagesPanelOpen) {
      setOpenMessageCount(0);
      setNewSinceLastVisit(0);
      setActiveAlerts(0);
    }
  }, [messagesPanelOpen]);
  // Load noise prefs from localStorage when the project changes; refresh from server async.
  useEffect(() => {
    if (!selectedProjectId) {
      closedPanelNoisePrefsRef.current = {};
      closedPanelTempMutesRef.current = {};
      return;
    }
    const pid = selectedProjectId;
    const userEmail = s.user?.email;
    closedPanelNoisePrefsRef.current = loadEventPrefs(userEmail, pid);
    closedPanelTempMutesRef.current = loadTemporaryMutes(userEmail, pid);
    fetchServerNoisePrefs(pid).then(serverPrefs => {
      if (serverPrefs !== null) {
        closedPanelNoisePrefsRef.current = resolveNoisePrefs(loadEventPrefs(userEmail, pid), serverPrefs);
      }
    }).catch(() => {});
    fetchServerTempMutes(pid).then(serverMutes => {
      if (serverMutes !== null) {
        closedPanelTempMutesRef.current = resolveServerTempMutes(loadTemporaryMutes(userEmail, pid), serverMutes);
      }
    }).catch(() => {});
  }, [selectedProjectId, s.user?.email]);

  // Re-read prefs from localStorage when panel closes (operator may have changed prefs while it was open).
  useEffect(() => {
    if (messagesPanelOpen || !selectedProjectId) return;
    closedPanelNoisePrefsRef.current = loadEventPrefs(s.user?.email, selectedProjectId);
    closedPanelTempMutesRef.current = loadTemporaryMutes(s.user?.email, selectedProjectId);
  }, [messagesPanelOpen, selectedProjectId, s.user?.email]);
  usePoller(async (signal) => {
    if (!selectedProjectId) return;
    const pid = selectedProjectId; // narrowed to string
    try {
      // Use the lightweight counts endpoint — returns only integers, no message previews.
      const localTs = loadLastSeen(s.user?.email, pid);
      const params = new URLSearchParams({ project_id: pid });
      if (localTs !== null) params.set('last_seen_ms', String(localTs));
      const res = await apiFetch(`${API_BASE_URL}/api/admin/messages/counts?${params}`);
      if (!res.ok || signal.aborted) return;
      const data = await res.json();
      if (!signal.aborted) {
        // Use the server-computed needs_operator count (blockers + questions)
        // from the attention model so the badge agrees with StrategicContextRail
        // and the in-panel needs-action set.
        setOpenMessageCount(data.needs_operator ?? 0);
        setNewSinceLastVisit(data.unread ?? 0);
        setActiveAlerts(data.active_alerts ?? 0);
      }
    } catch { /* ignore */ }
  }, {
    intervalMs: DEFAULT_POLL_MS,
    enabled: !!selectedProjectId && !messagesPanelOpen,
    deps: [selectedProjectId, messagesPanelOpen, s.user?.email],
  });
  // New project modal
  const [showNewProjectModal, setShowNewProjectModal] = useState(false);
  const [showProjectManagement, setShowProjectManagement] = useState(false);
  const [setupProjectId, setSetupProjectId] = useState<string | null>(null);
  const setupProject = useMemo(() => {
    if (!setupProjectId) return undefined;
    const p = s.projects.find(proj => proj.id === setupProjectId);
    if (!p) return undefined;
    return { id: p.id, owner: p.github_owner, repo: p.github_repo };
  }, [setupProjectId, s.projects]);
  const [showActivityFeed, setShowActivityFeed] = useState(false);
  // Project members panel
  const [showProjectMembers, setShowProjectMembers] = useState(false);
  const selectedProject = useMemo(
    () => s.projects.find(p => p.id === s.selectedProjectId) ?? null,
    [s.projects, s.selectedProjectId],
  );

  // Per-tab data-arrival epochs so board_data_ready fires on each tab's own
  // primary data, not on the shared tasks fetchEpoch.
  const [projectDataEpoch, setProjectDataEpoch] = useState(0);
  const [runtimeDataEpoch, setRuntimeDataEpoch] = useState(0);
  useEffect(() => {
    if (selectedProject !== null) setProjectDataEpoch(e => e + 1);
  }, [selectedProject]);
  useEffect(() => {
    if (s.orchestratorHealth !== null) setRuntimeDataEpoch(e => e + 1);
  }, [s.orchestratorHealth]);

  // Route the correct epoch/loading signals to BoardDataReadyReporter per tab.
  const boardDataEpoch = activeTab === 'project' ? projectDataEpoch : activeTab === 'runtime' ? runtimeDataEpoch : s.fetchEpoch;
  const boardInitialLoading = activeTab === 'project' ? !s.projectsLoaded : activeTab === 'runtime' ? s.orchestratorHealth === null : s.initialLoading;
  const boardRefreshing = (activeTab === 'project' || activeTab === 'runtime') ? false : s.refreshing;

  const selectedProjectRepoLabel = selectedProject
    ? `${selectedProject.github_owner}/${selectedProject.github_repo}`
    : null;
  const selectedProjectAutonomous = selectedProject?.autonomous_enabled ?? false;
  // Command-palette action: flip the selected project's autonomous mode. Reuses
  // the existing PATCH endpoint (no new API) and refreshes the projects list so
  // the header AutonomousStatus and the command's own label stay in sync.
  const toggleSelectedProjectAutonomy = useCallback(async () => {
    const pid = s.selectedProjectId;
    if (!pid) return;
    const next = !(selectedProject?.autonomous_enabled ?? false);
    try {
      const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(pid)}/autonomous`, {
        method: 'PATCH',
        body: JSON.stringify({ enabled: next }),
      });
      if (!res.ok) throw new Error('Failed to toggle autonomous mode');
      s.addToast(`Autonomous mode ${next ? 'enabled' : 'paused'}`, 'success');
      void s.fetchProjects(true);
    } catch {
      s.addToast('Failed to toggle autonomous mode', 'error');
    }
  }, [s, selectedProject]);
  // Fetch project members when panel opens or project changes
  const { fetchProjectMembers } = s;
  useEffect(() => {
    if (showProjectMembers && selectedProjectId) {
      fetchProjectMembers(selectedProjectId);
    }
  }, [showProjectMembers, selectedProjectId, fetchProjectMembers]);
  const handleViewTask = useCallback((taskId: string) => {
    const task = s.tasks.find(t => t.id === taskId);
    if (task) handleOpenTaskDetails(task);
    else fetchTask(taskId).then(handleOpenTaskDetails).catch(() => {});
  }, [handleOpenTaskDetails, s.tasks]);

  const [dismissedZoneAlertIds, setDismissedZoneAlertIds] = useState<Set<string>>(new Set());
  const [researchBrowserDirectionId, setResearchBrowserDirectionId] = useState<string | null>(null);
  const [knowledgeBrowserDirectionId, setKnowledgeBrowserDirectionId] = useState<string | null>(null);
  const [knowledgeBrowserInitialDocId, setKnowledgeBrowserInitialDocId] = useState<string | null>(null);
  const [researchFeed, setResearchFeed] = useState<Task[]>([]);
  // Fetch project tasks when the Findings journal opens so findings and implementation child
  // outcomes are independent of the main paginated table state.
  useEffect(() => {
    if (!s.showResearchBrowser || !selectedProjectId) { setResearchFeed([]); return; }
    const pid = selectedProjectId;
    let cancelled = false;
    apiFetch(
      `${API_BASE_URL}/api/admin/tasks?task_type=research&archived=false&limit=500&project_id=${encodeURIComponent(pid)}`,
    )
      .then(async res => {
        if (!res.ok || cancelled) return;
        const data = await res.json();
        const all: Task[] = Array.isArray(data) ? data : (data.tasks ?? []);
        if (!cancelled) setResearchFeed(all);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [s.showResearchBrowser, selectedProjectId]);
  const zoneHealthAlerts = useMemo(() => {
    if (!s.zoneHealth) return [];
    const zones = Object.entries(s.zoneHealth);
    const exhaustedZones = zones.filter(([, z]) => z.status === 'exhausted').map(([name]) => name);
    const cooldownZones = zones.filter(([, z]) => z.cooldown_active && z.status !== 'exhausted').map(([name]) => name);
    const allInCooldown = zones.length > 0 && cooldownZones.length + exhaustedZones.length === zones.length;
    const alerts: import('./components/AlertsBanner').Alert[] = [];
    if (exhaustedZones.length > 0) {
      alerts.push({
        id: `zone-exhausted-${exhaustedZones.join(',')}`,
        project_id: '',
        severity: 'ERROR',
        title: 'Zone Capacity Exhausted',
        message: `${exhaustedZones.length === 1 ? 'Zone' : 'Zones'} ${exhaustedZones.join(', ')} ${exhaustedZones.length === 1 ? 'is' : 'are'} exhausted and cannot accept new tasks.`,
        task_id: null,
        created_at: new Date().toISOString(),
        dismissed_at: null,
        acknowledged_at: null,
      });
    }
    if (allInCooldown && exhaustedZones.length === 0) {
      alerts.push({
        id: `zone-all-cooldown`,
        project_id: '',
        severity: 'WARNING',
        title: 'All Zones in Cooldown',
        message: `All compute zones are in cooldown: ${cooldownZones.join(', ')}. New tasks may be delayed.`,
        task_id: null,
        created_at: new Date().toISOString(),
        dismissed_at: null,
        acknowledged_at: null,
      });
    } else if (cooldownZones.length > 0 && exhaustedZones.length === 0) {
      alerts.push({
        id: `zone-cooldown-${cooldownZones.join(',')}`,
        project_id: '',
        severity: 'WARNING',
        title: 'Zone Cooldown Active',
        message: `${cooldownZones.length === 1 ? 'Zone' : 'Zones'} ${cooldownZones.join(', ')} ${cooldownZones.length === 1 ? 'is' : 'are'} in cooldown.`,
        task_id: null,
        created_at: new Date().toISOString(),
        dismissed_at: null,
        acknowledged_at: null,
      });
    }
    return alerts.filter(a => !dismissedZoneAlertIds.has(a.id));
  }, [s.zoneHealth, dismissedZoneAlertIds]);

  const combinedAlerts = useMemo(() => [...zoneHealthAlerts, ...s.alerts], [zoneHealthAlerts, s.alerts]);

  const strategicReadModel = useStrategicReadModel(s.selectedProjectId, { pollerEnabled: chatPollersEnabled });
  const strategicLiveness = useStrategicLiveness(s.selectedProjectId, { enabled: chatPollersEnabled });
  const {
    strategicWorkbenchState, strategicInteractionMode, strategicSessionActive,
    strategicSessionStatus, strategicSessionState, strategicSessionReason,
    residentStrategicSessionRunners, strategicSessionSelectedRunnerId,
    savingStrategicWorkbench, strategicSessionBusy, persistStrategicWorkbench,
    handleSelectStrategicRunner, handleStartStrategicSession, handleStopStrategicSession,
  } = useStrategicSession({
    selectedProjectId, addToast: s.addToast, strategicReadModel,
    strategicLiveness: strategicLiveness.data,
  });
  const setStrategicInteractionMode = useCallback((mode: StrategicInteractionMode) => {
    persistStrategicWorkbench({ interaction_mode: mode, active_surface: 'strategic' });
  }, [persistStrategicWorkbench]);
  // Monotonic epoch that advances each time the strategic-state poll delivers
  // new data. Threaded into MessagesPanel → useConversation so the
  // conversation refresh piggybacks on the strategic-state poll cadence instead
  // of running its own independent timer (eliminating the duplicate assembly).
  const strategicPollEpochRef = useRef(0);
  const [strategicPollEpoch, setStrategicPollEpoch] = useState(0);
  useEffect(() => {
    if (strategicReadModel.data) {
      strategicPollEpochRef.current += 1;
      setStrategicPollEpoch(strategicPollEpochRef.current);
    }
  }, [strategicReadModel.data]);
  useEffect(() => {
    refreshStrategicReadModelRef.current = () => {
      void strategicReadModel.refresh();
    };
    return () => {
      refreshStrategicReadModelRef.current = () => {};
    };
  }, [strategicReadModel.refresh]);
  const pipelineAttentionCount = useMemo(() => {
    const counts = strategicReadModel.data?.attention?.counts;
    if (!counts) return s.needsAttentionCount;
    return (
      (counts.approvals ?? 0) +
      (counts.local_runner_holds ?? 0) +
      (counts.stale_runner_pins ?? 0) +
      (counts.stale_provisioning_tasks ?? 0) +
      (counts.stale_queued_tasks ?? 0) +
      (counts.failed_tasks ?? 0) +
      (counts.blocked_tasks ?? 0) +
      (counts.parked_tasks ?? 0) +
      (counts.pending_confirmations ?? 0)
    );
  }, [s.needsAttentionCount, strategicReadModel.data?.attention?.counts]);
  const { open: commandPaletteOpen, query: commandPaletteQuery, setOpen: setCommandPaletteOpen, setQuery: setCommandPaletteQuery } = useCommandPaletteState();
  const [shortcutHelpOpen, setShortcutHelpOpen] = useState(false);
  const [strategicContextWorkspaceOpen, setStrategicContextWorkspaceOpen] = useState(false);
  // Chat right-rail (StrategicContextRail) is collapsible and hidden by default so the
  // conversation gets full width; the operator opens it on demand. Persisted per-browser.
  const [chatRailCollapsed, setChatRailCollapsed] = useState(() => {
    try { return localStorage.getItem('bp.chatRailCollapsed') !== 'false'; } catch { return true; }
  });
  const toggleChatRail = useCallback(() => {
    setChatRailCollapsed(prev => {
      const next = !prev;
      try { localStorage.setItem('bp.chatRailCollapsed', String(next)); } catch { /* ignore */ }
      return next;
    });
  }, []);
  const [strategicComposerFocusSignal, setStrategicComposerFocusSignal] = useState(0);
  useEffect(() => {
    setStrategicContextWorkspaceOpen(false);
  }, [selectedProjectId]);
  const selectedTasks = useMemo(() => s.tasks.filter(t => s.selectedTaskIds.has(t.id)), [s.tasks, s.selectedTaskIds]);
  const cancellableTasks = useMemo(() => selectedTasks.filter(t => CANCELLABLE_STATUSES.has(t.status)), [selectedTasks]);
  const failedTasks = useMemo(() => selectedTasks.filter(t => t.status === 'FAILED'), [selectedTasks]);
  const terminalTasks = useMemo(() => selectedTasks.filter(t => TERMINAL_STATUSES.has(t.status)), [selectedTasks]);
  const approvableTasks = useMemo(() => selectedTasks.filter(t => t.requires_approval && !TERMINAL_STATUSES.has(t.status)), [selectedTasks]);
  const effectiveColumnPrefs = useMemo<ColumnPref[]>(() => {
    const base = s.columnPrefs.length > 0 ? s.columnPrefs : COLUMNS.map(c => ({ id: c.id, visible: true }));
    // Add any COLUMNS entry not yet in prefs (e.g. newly added columns)
    const inPrefs = new Set(base.map(p => p.id));
    const extra = COLUMNS.filter(c => !inPrefs.has(c.id)).map(c => ({ id: c.id, visible: true }));
    return [...base, ...extra];
  }, [s.columnPrefs]);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const columnMap = useMemo(() => new Map<string, typeof COLUMNS[number]>(COLUMNS.map(c => [c.id, c])), []);
  const visibleColumns = useMemo(() => {
    return effectiveColumnPrefs
      .filter(pref => pref.visible)
      .map(pref => columnMap.get(pref.id))
      .filter((c): c is typeof COLUMNS[number] => !!c)
      .filter(col => col.includes.some(status => ACTIVE_TASK_STATUS_FILTER.includes(status)));
  }, [effectiveColumnPrefs, columnMap]);

  const stuckTaskIds = useMemo(() => new Set((s.diagnostics?.stuck_tasks ?? []).filter((st: { task_id: string; stuck_for_seconds: number }) => st.stuck_for_seconds >= s.stuckMinSeconds).map((st: { task_id: string }) => st.task_id)), [s.diagnostics, s.stuckMinSeconds]);
  const dateRangeCutoffs: Record<string, number> = { '1h': 3600000, '6h': 21600000, '24h': 86400000, '7d': 604800000 };
  const filteredTasks = useMemo(() => {
    const now = new Date().getTime();
    const cutoff = s.dateRange !== 'all' ? now - dateRangeCutoffs[s.dateRange] : null;
    return s.tasks.filter(t => {
      const taskType = t.task_type || (t.payloads?.[0] as Record<string, unknown>)?.type as string || 'create';
      const typeMatch = matchesTaskTypeFilter(taskType, s.taskTypeFilter, filterTaskTypeIds);
      const statusMatch = matchesStatusFilter(t.status, ACTIVE_TASK_STATUS_FILTER);
      const query = s.searchQuery.toLowerCase();
      const searchMatch = !query || t.id.toLowerCase().includes(query) || (t.title?.toLowerCase().includes(query)) || (t.description?.toLowerCase().includes(query)) || (t.latest_status_message?.toLowerCase().includes(query));
      const projectMatch = s.selectedProjectId === null || t.project_id === s.selectedProjectId;
      const dateMatch = cutoff === null || parseTimestamp(t.created_at).getTime() >= cutoff;
      // Prefer the first-class BLOCKED status; fall back to the legacy
      // QUEUED+depends_on inference for older payloads predating the status.
      const blockedMatch = !s.blockedFilter || t.status === 'BLOCKED' || (t.status === 'QUEUED' && t.depends_on != null && t.depends_on.length > 0);
      const failureClassMatch = s.failureClassFilter.length === 0 || (t.status === 'FAILED' && t.failure_class != null && s.failureClassFilter.includes(t.failure_class));
      return typeMatch && statusMatch && searchMatch && projectMatch && dateMatch && blockedMatch && failureClassMatch;
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [s.tasks, s.taskTypeFilter, s.searchQuery, s.selectedProjectId, s.dateRange, s.blockedFilter, s.failureClassFilter, filterTaskTypeIds]);

  const {
    addToast: addTaskLookupToast,
    tasks: taskLookupTasks,
  } = s;
  const selectPipelineTaskById = useCallback((taskId: string) => {
    const target = taskLookupTasks.find(t => t.id === taskId);
    const requestId = pipelineContextRequestRef.current + 1;
    pipelineContextRequestRef.current = requestId;
    if (target) {
      setPipelineContextTask(target);
      return;
    }
    setPipelineContextTask(null);
    fetchTask(taskId)
      .then(task => {
        if (pipelineContextRequestRef.current === requestId) setPipelineContextTask(task);
      })
      .catch(() => {
        if (pipelineContextRequestRef.current === requestId) {
          addTaskLookupToast('Could not load task context — it may have been deleted', 'error');
        }
      });
  }, [addTaskLookupToast, taskLookupTasks]);
  const handleViewTaskById = useCallback((taskId: string) => {
    const target = taskLookupTasks.find(t => t.id === taskId);
    if (target) {
      handleOpenTaskDetails(target);
      return;
    }
    fetchTask(taskId)
      .then(handleOpenTaskDetails)
      .catch(() => {
        addTaskLookupToast('Could not load task — it may have been deleted', 'error');
      });
  }, [addTaskLookupToast, handleOpenTaskDetails, taskLookupTasks]);

  const hasActiveFilters = s.tasks.length > 0 && (filteredTasks.length < s.tasks.length || s.failureClassFilter.length > 0);
  const { visibleActiveTasks, visibleQueuedTasks, visibleBlockedTasks, visibleFailedTasks, visibleTerminalTasks } = useMemo(() => {
    const active: typeof filteredTasks = [];
    const queued: typeof filteredTasks = [];
    const blocked: typeof filteredTasks = [];
    const failed: typeof filteredTasks = [];
    const terminal: typeof filteredTasks = [];
    for (const t of filteredTasks) {
      if (ACTIVE_PIPELINE_STATUSES.has(t.status)) active.push(t);
      if (t.status === 'QUEUED') queued.push(t);
      if (t.status === 'BLOCKED') blocked.push(t);
      if (t.status === 'FAILED') failed.push(t);
      if (TERMINAL_STATUSES.has(t.status)) terminal.push(t);
    }
    return { visibleActiveTasks: active, visibleQueuedTasks: queued, visibleBlockedTasks: blocked, visibleFailedTasks: failed, visibleTerminalTasks: terminal };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filteredTasks]);
  const backendPipelineAttentionItems = useMemo(() => {
    const byTaskId = new Map<string, StrategicStateAttentionItem>();
    (strategicReadModel.data?.attention?.items ?? []).forEach(item => {
      const taskId = pipelineTaskIdForAttentionItem(item);
      if (taskId && !byTaskId.has(taskId)) byTaskId.set(taskId, item);
    });
    return Array.from(byTaskId.entries());
  }, [strategicReadModel.data?.attention?.items]);
  const backendAttentionTasks = useMemo(() => {
    const taskById = new Map(s.tasks.map(task => [task.id, task]));
    return backendPipelineAttentionItems
      .map(([taskId, attention]) => {
        const task = taskById.get(taskId);
        return task ? { task, attention } : null;
      })
      .filter((entry): entry is { task: Task; attention: StrategicStateAttentionItem } => Boolean(entry));
  }, [backendPipelineAttentionItems, s.tasks]);
  const localAttentionTasks = useMemo(() => {
    const priority = (task: Task) => {
      if (task.status === 'FAILED') return 5;
      if (task.status === 'BLOCKED') return 4;
      if (stuckTaskIds.has(task.id)) return 3;
      if (ACTIVE_PIPELINE_STATUSES.has(task.status)) return 2;
      return 1;
    };
    return [...filteredTasks]
      .filter(t => t.status === 'FAILED' || t.status === 'BLOCKED' || stuckTaskIds.has(t.id) || ACTIVE_PIPELINE_STATUSES.has(t.status))
      .sort((a, b) => priority(b) - priority(a) || parseTimestamp(b.created_at).getTime() - parseTimestamp(a.created_at).getTime());
  }, [filteredTasks, stuckTaskIds]);
  const selectedPipelineContextTask = useMemo(() => {
    if (!pipelineContextTask) return null;
    const refreshedTask = taskLookupTasks.find(t => t.id === pipelineContextTask.id);
    const task = refreshedTask ?? pipelineContextTask;
    if (s.selectedProjectId && task.project_id !== s.selectedProjectId) return null;
    return task;
  }, [pipelineContextTask, s.selectedProjectId, taskLookupTasks]);
  const openPipelineTask = useMemo(() => {
    if (selectedPipelineContextTask && s.logTask?.id === selectedPipelineContextTask.id) return s.logTask;
    if (selectedPipelineContextTask) return selectedPipelineContextTask;
    if (!s.logTask) return null;
    if (s.selectedProjectId && s.logTask.project_id !== s.selectedProjectId) return null;
    return s.logTask;
  }, [s.logTask, s.selectedProjectId, selectedPipelineContextTask]);

  const [pipelineSection, setPipelineSection] = useState<PipelineSection>('flow');
  const [projectSection, setProjectSection] = useState<ProjectSection>('running-update');
  const [projectManageSection, setProjectManageSection] = useState<ProjectManageSection>('overview');
  const [runtimeSection, setRuntimeSection] = useState<RuntimeSection>('health');
  const [runtimeConfigSection, setRuntimeConfigSection] = useState<RuntimeSettingsSection>('orchestrator');
  const { versionLifecycle, versionLifecycleError, versionLifecycleLastSuccessAt, versionLifecycleRefreshing, refreshVersionLifecycle } = useVersionLifecycle(s.selectedProjectId, { pollerEnabled: runtimePollersEnabled });
  const hasVersionLifecycle = hasVersionLifecycleData(versionLifecycle);
  const { setActiveTab: setPrimaryTab, setShowArchive: setArchiveVisible, setViewMode: setTaskViewMode } = s;
  const applyPipelineSection = useCallback((section: PipelineSection) => {
    setPipelineSection(section);
    setArchiveVisible(section === 'archive');
    if (section === 'flow') setTaskViewMode('pipeline');
    if (section === 'board') setTaskViewMode('kanban');
  }, [setArchiveVisible, setTaskViewMode]);
  const cycleCurrentWorkbenchSection = useCallback((direction: -1 | 1) => {
    if (activeTab === 'pipeline') {
      applyPipelineSection(cycleSection(PIPELINE_SECTION_ORDER, pipelineSection, direction));
      return true;
    }
    if (activeTab === 'project') {
      setProjectSection(cycleSection(PROJECT_SECTION_ORDER, projectSection, direction));
      return true;
    }
    if (activeTab === 'runtime') {
      const visibleRuntimeSections = RUNTIME_SECTION_ORDER.filter(
        s => s !== 'version-lifecycle' || hasVersionLifecycle,
      );
      setRuntimeSection(cycleSection(visibleRuntimeSections, runtimeSection, direction));
      return true;
    }
    return false;
  }, [activeTab, applyPipelineSection, hasVersionLifecycle, pipelineSection, projectSection, runtimeSection]);
  useEffect(() => {
    if (activeTab !== 'pipeline') return;
    if (s.showArchive) {
      setPipelineSection('archive');
      return;
    }
    setPipelineSection(s.viewMode === 'kanban' ? 'board' : 'flow');
  }, [activeTab, s.showArchive, s.viewMode]);
  useEffect(() => {
    const target = resolvePrimaryTabTarget(rawActiveTab);
    if (target.projectSection) setProjectSection(target.projectSection);
    if (target.runtimeSection) setRuntimeSection(target.runtimeSection);
    if (target.pipelineSection) setPipelineSection(target.pipelineSection);
    if (target.viewMode) setTaskViewMode(target.viewMode);
    if (target.showArchive !== undefined) setArchiveVisible(target.showArchive);
    if (target.tab !== rawActiveTab) setPrimaryTab(target.tab);
  }, [rawActiveTab, setArchiveVisible, setPrimaryTab, setTaskViewMode]);
  const allModelOptions = useMemo(() => {
    const models = new Set<string>();
    Object.values(s.providerTierModelMap).forEach(tiers => {
      Object.values(tiers).forEach(model => { if (model) models.add(model); });
    });
    Object.values(s.taskTypeModelPreferences).forEach(prefs => {
      prefs.forEach(pref => { if (pref.model) models.add(pref.model); });
    });
    Object.values(s.providerSettingsDefaults).forEach(config => {
      if (typeof config.model === 'string') models.add(config.model);
    });
    return Array.from(models).sort();
  }, [s.providerTierModelMap, s.taskTypeModelPreferences, s.providerSettingsDefaults]);
  const [healthCheckUrlsInput, setHealthCheckUrlsInput] = useState(() => s.healthCheckUrls.join('\n'));
  useEffect(() => {
    setHealthCheckUrlsInput(s.healthCheckUrls.join('\n'));
  }, [s.healthCheckUrls]);
  const ignoreEmbeddedSettingsClose = useCallback(() => {}, []);
  // useCallback for inline closures passed to PipelineWorkspace props
  const handleNavigateToParent = useCallback((parentTaskId: string) => {
    navigator.clipboard.writeText(parentTaskId).then(() => {
      addToast('Parent task ID copied — use search to find it', 'info');
    }).catch(() => {
      addToast(`Could not copy to clipboard — parent task ID: ${parentTaskId}`, 'error');
    });
  }, [addToast]);
  const handleArchiveGroup = useCallback((taskIds: string[]) => {
    s.setConfirmDialog({
      title: 'Archive this group?',
      message: `This will archive ${taskIds.length} task${taskIds.length !== 1 ? 's' : ''} (parent + children). They can be viewed and unarchived from the Archive view.`,
      confirmLabel: 'Archive group',
      onConfirm: () => s.handleBulkArchiveColumn(taskIds),
    });
  }, [s.setConfirmDialog, s.handleBulkArchiveColumn]);
  const handleBoardCtrlClick = useCallback((taskId: string) => {
    if (!s.selectionMode) s.setSelectionMode(true);
    s.toggleTaskSelection(taskId);
  }, [s.selectionMode, s.setSelectionMode, s.toggleTaskSelection]);
  const handleBoardArchiveColumn = useCallback((taskIds: string[], columnLabel: string) => {
    s.setConfirmDialog({
      title: `Archive all ${columnLabel} tasks?`,
      message: `This will archive ${taskIds.length} task${taskIds.length !== 1 ? 's' : ''} in the ${columnLabel} column. They can be viewed and unarchived from the Archive view.`,
      confirmLabel: `Archive ${columnLabel}`,
      onConfirm: () => s.handleBulkArchiveColumn(taskIds),
    });
  }, [s.setConfirmDialog, s.handleBulkArchiveColumn]);
  const handleBoardDirectionClick = useCallback((dirId: string) => {
    setResearchBrowserDirectionId(dirId);
    s.setShowResearchBrowser(true);
  }, [setResearchBrowserDirectionId, s.setShowResearchBrowser]);
  const handleClearWaveDeps = useMemo(() => {
    const pid = s.selectedProjectId;
    return pid ? () => s.handleClearWaveDeps(pid) : undefined;
  }, [s.selectedProjectId, s.handleClearWaveDeps]);
  const settingsRouterProps = useSettingsRouterProps({
    s,
    allModelOptions,
    healthCheckUrlsInput,
    onHealthCheckUrlsInputChange: setHealthCheckUrlsInput,
    setRuntimeSection,
    setRuntimeConfigSection,
    onToggleSettings: ignoreEmbeddedSettingsClose,
    showProjectMembers,
    setShowProjectMembers,
    selectedProject,
  });

  // activeTab drives the four operator modes: Strategic, Pipeline, Project, Runtime.
  const handleTabChange = useCallback((tab: PrimaryTab) => {
    if (tab !== 'pipeline') {
      s.setShowArchive(false);
    }
    s.setActiveTab(tab);
  }, [s]);

  const openTrackedTaskStreams = useCallback(() => {
    setArchiveVisible(false);
    setTaskViewMode('pipeline');
    setPipelineSection('flow');
    handleTabChange('pipeline');
  }, [handleTabChange, setArchiveVisible, setTaskViewMode]);

  const openRuntimeAccounts = useCallback(() => {
    setRuntimeSection('accounts');
    handleTabChange('runtime');
  }, [handleTabChange]);

  const openRuntimeAnalytics = useCallback(() => {
    setRuntimeSection('analytics');
    handleTabChange('runtime');
  }, [handleTabChange]);

  const openRuntimeSettings = useCallback((section: RuntimeSettingsSection = 'general') => {
    void s.refreshSettings();
    setRuntimeSection('settings');
    setRuntimeConfigSection(section);
    handleTabChange('runtime');
    setCommandPaletteOpen(false);
  }, [handleTabChange, s, setCommandPaletteOpen]);

  const openProjectManageWorkspace = useCallback((section: ProjectManageSection, projectId?: string) => {
    if (projectId && projectId !== s.selectedProjectId) s.setSelectedProjectId(projectId);
    setProjectSection('manage');
    setProjectManageSection(section);
    handleTabChange('project');
  }, [handleTabChange, s]);

  const handleConfigureTargets = useCallback(() => {
    openProjectManageWorkspace('build-config');
    window.setTimeout(() => {
      const element = document.getElementById('sr-catch-rate-targets');
      element?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, 100);
  }, [openProjectManageWorkspace]);

  const handleConfigureBranchProject = useCallback((projectId: string) => {
    openProjectManageWorkspace('branches', projectId);
  }, [openProjectManageWorkspace]);

  const focusStrategicComposer = useCallback(() => {
    handleTabChange('chat'); setStrategicComposerFocusSignal(value => value + 1); setCommandPaletteOpen(false);
  }, [handleTabChange, setCommandPaletteOpen]);
  const strategicMessageTarget = useStrategicMessageTarget(() => handleTabChange('chat'), selectedProjectId);
  const openStrategicContextTarget = useStrategicContextNavigation({
    handleTabChange, openMessages: strategicMessageTarget.open, openTask: handleViewTaskById, selectPipelineTaskById,
    setKnowledgeBrowserDirectionId, setKnowledgeBrowserInitialDocId,
    setPipelineSection, setProjectSection, setProjectManageSection, setRuntimeSection, setRuntimeConfigSection,
    setShowArchive: s.setShowArchive, setShowKnowledgeBrowser: s.setShowKnowledgeBrowser, setViewMode: s.setViewMode,
  });
  const openStrategicContextWorkspace = useCallback(() => {
    setCommandPaletteOpen(false);
    setStrategicContextWorkspaceOpen(true);
  }, [setCommandPaletteOpen]);
  const closeStrategicContextWorkspace = useCallback(() => setStrategicContextWorkspaceOpen(false), []);
  const openStrategicContextWorkspaceTarget = useCallback((target: Parameters<typeof openStrategicContextTarget>[0]) => {
    setStrategicContextWorkspaceOpen(false);
    openStrategicContextTarget(target);
  }, [openStrategicContextTarget]);
  const openStrategicMessageTargetFromCommandPalette = useCallback((target?: Parameters<typeof strategicMessageTarget.open>[0]) => {
    setStrategicContextWorkspaceOpen(false);
    strategicMessageTarget.open(target);
  }, [strategicMessageTarget.open]);
  const openPreviousWorkbenchSubtab = useCallback(() => {
    cycleCurrentWorkbenchSection(-1);
  }, [cycleCurrentWorkbenchSection]);
  const openNextWorkbenchSubtab = useCallback(() => {
    cycleCurrentWorkbenchSection(1);
  }, [cycleCurrentWorkbenchSection]);
  const strategicAccountSpend = useMemo<StrategicAccountSpendStatus>(() => {
    const accounts = s.maxPlanAccounts ?? [];
    const cooldownAccounts = accounts.filter(account => Object.keys(account.disabled_models ?? {}).length > 0).length;
    const unavailableStatuses = new Set(['disabled', 'unconfigured', 'unavailable', 'at_cap']);
    const availableAccounts = accounts.filter(account => {
      const status = typeof account.account_status === 'string' ? account.account_status.toLowerCase() : null;
      return account.enabled
        && !account.disabled_reason
        && account.credential_configured !== false
        && account.runtime_ready !== false
        && Object.keys(account.disabled_models ?? {}).length === 0
        && (!status || !unavailableStatuses.has(status));
    }).length;
    return {
      totalAccounts: accounts.length,
      availableAccounts,
      enabledAccounts: accounts.filter(account => account.enabled).length,
      activeAccountTasks: accounts.reduce((sum, account) => sum + (account.active_tasks ?? 0), 0),
      cooldownAccounts,
      configuredCapAccounts: accounts.filter(account => account.daily_task_cap != null || account.max_concurrent_tasks != null).length,
      disabledModelCount: accounts.reduce((sum, account) => sum + Object.keys(account.disabled_models ?? {}).length, 0),
      trackedSpendUsd: s.cumulativeCost,
      dailyCostCeilingUsd: s.dailyCostCeilingUsd,
    };
  }, [s.cumulativeCost, s.dailyCostCeilingUsd, s.maxPlanAccounts]);

  const workbenchCommands = useWorkbenchCommands({
    selectedProjectId: s.selectedProjectId,
    selectedProjectRepoLabel,
    hasVersionLifecycle,
    autonomousEnabled: selectedProjectAutonomous,
    toggleAutonomousMode: toggleSelectedProjectAutonomy,
    directionsCount: directions.length,
    filteredTaskCount: filteredTasks.length,
    visibleActiveCount: visibleActiveTasks.length,
    visibleQueuedCount: visibleQueuedTasks.length,
    maxPlanAccountCount: s.maxPlanAccounts.length,
    apiHealthy: s.apiHealthy,
    isAdmin: s.userRole === 'admin',
    problemModelRevision: strategicReadModel.data?.problem_model.revision,
    hasDesignDoc: strategicReadModel.data?.design.has_design_doc,
    designRevision: strategicReadModel.data?.design.revision,
    strategicState: strategicReadModel.data ?? null,
    focusStrategicComposer,
    openStrategicContextTarget: openStrategicContextWorkspaceTarget,
    openStrategicContextWorkspace,
    openStrategicMessageTarget: openStrategicMessageTargetFromCommandPalette,
    handleTabChange,
    setCommandPaletteOpen,
    setPipelineSection,
    setProjectSection,
    setProjectManageSection,
    setRuntimeSection,
    setRuntimeConfigSection,
    setViewMode: s.setViewMode,
    setShowArchive: s.setShowArchive,
    setShowActivityFeed,
    setShowResearchBrowser: s.setShowResearchBrowser,
    setShowKnowledgeBrowser: s.setShowKnowledgeBrowser,
    openNewTaskModal: s.openNewTaskModal,
    refreshSettings: s.refreshSettings,
    refreshWorkbench: () => {
      void s.fetchTasks();
      void strategicReadModel.refresh();
    },
    openProjectManagement: () => setShowProjectManagement(true),
    openProjectMembers: () => setShowProjectMembers(true),
    openPreviousWorkbenchSubtab,
    openNextWorkbenchSubtab,
  });
  const modalShortcutBlocked = Boolean(
    s.showNewTaskModal ||
    s.confirmDialog ||
    showNewProjectModal ||
    setupProjectId ||
    showProjectManagement ||
    showProjectMembers,
  );
  const operatorShortcutBlocked = Boolean(
    modalShortcutBlocked ||
    s.logTask ||
    s.showFleetDetail ||
    strategicContextWorkspaceOpen,
  );
  const transientWorkbenchPanelOpen = Boolean(
    s.showResearchBrowser ||
    s.showKnowledgeBrowser ||
    showActivityFeed,
  );

  const { openCommandPalette } = useOperatorKeyboardShortcuts({
    activeTab,
    blockingModalOpen: operatorShortcutBlocked,
    cycleCurrentSection: cycleCurrentWorkbenchSection,
    commandPaletteShortcutBlocked: modalShortcutBlocked,
    commandPaletteOpen, commands: workbenchCommands, searchInputRef,
    searchShortcutBlocked: Boolean(s.showNewTaskModal || s.logTask || transientWorkbenchPanelOpen),
    selectedTaskCount: s.selectedTaskIds.size, selectionMode: s.selectionMode, setCommandPaletteOpen,
    setKnowledgeBrowserDirectionId,
    setKnowledgeBrowserInitialDocId,
    setResearchBrowserDirectionId,
    setShortcutHelpOpen,
    setShowActivityFeed,
    setShowKnowledgeBrowser: s.setShowKnowledgeBrowser,
    setShowResearchBrowser: s.setShowResearchBrowser,
    shortcutHelpOpen,
    transientPanelOpen: transientWorkbenchPanelOpen,
  });

  const pipelineSurfaceActions = (
    <>
      <span className="hidden items-center rounded-md border border-neutral-800 bg-neutral-950/80 px-2.5 py-1.5 text-xs text-neutral-500 md:inline-flex">
        {filteredTasks.length}/{s.tasks.length}
      </span>
      <button
        type="button"
        onClick={() => { void s.fetchTasks(); }}
        className="inline-flex min-h-11 items-center gap-1.5 rounded-md border border-neutral-800 bg-neutral-950/70 px-3 text-xs font-semibold text-neutral-300 transition-colors hover:border-neutral-700 hover:bg-neutral-900 hover:text-neutral-100 sm:min-h-9"
      >
        Refresh
      </button>
      <button
        type="button"
        onClick={s.openNewTaskModal}
        className="inline-flex min-h-11 items-center gap-1.5 rounded-md border border-neutral-800 bg-neutral-950/70 px-3 text-xs font-semibold text-neutral-300 transition-colors hover:border-neutral-700 hover:bg-neutral-900 hover:text-neutral-100 sm:min-h-9"
      >
        New task
      </button>
    </>
  );

  const pipelineFilterStrip = (
    <div className="shrink-0 border-b border-neutral-800 bg-[#07090c] px-3 py-2 md:px-4">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <SearchFilterBar
          searchQuery={s.searchQuery}
          onSearchChange={s.setSearchQuery}
          taskTypeFilter={s.taskTypeFilter}
          onTypeFilterChange={s.setTaskTypeFilter}
          failureClassFilter={s.failureClassFilter}
          onFailureClassFilterChange={s.setFailureClassFilter}
          dateRange={s.dateRange}
          onDateRangeChange={s.setDateRange}
          blockedFilter={s.blockedFilter}
          onBlockedFilterChange={s.setBlockedFilter}
          directionFilter={s.directionFilter}
          onDirectionFilterChange={s.setDirectionFilter}
          directions={directions}
          inputRef={searchInputRef}
        />
        {s.viewMode === 'kanban' && (
          <SingleSelectDropdown
            options={[
              { label: 'Newest first', value: 'newest' },
              { label: 'Oldest first', value: 'oldest' },
              { label: 'Type', value: 'type' },
              { label: 'Title', value: 'title' },
              { label: 'Longest running', value: 'duration' },
              { label: 'Most retried', value: 'attempts' },
            ]}
            value={sortBy}
            onChange={v => setSortBy(v as SortBy)}
          />
        )}
        {s.viewMode === 'kanban' && (
          <ColumnsPicker prefs={effectiveColumnPrefs} onPrefsChange={s.setColumnPrefs} columns={COLUMNS} />
        )}
        {s.viewMode === 'kanban' && (
          <button
            type="button"
            onClick={() => s.setCompactMode(v => !v)}
            title={s.compactMode ? 'Switch to normal density' : 'Switch to compact density'}
            className={`inline-flex min-h-11 items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors ${
              s.compactMode
                ? 'border-cyan-500/30 bg-cyan-500/10 text-cyan-300'
                : 'border-neutral-800/50 bg-neutral-900/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300'
            }`}
          >
            <LayoutList className="h-3.5 w-3.5" />
            {s.compactMode ? 'Compact' : 'Normal'}
          </button>
        )}
        {s.viewMode === 'kanban' && (
          <button
            type="button"
            onClick={s.toggleSelectionMode}
            title={s.selectionMode ? 'Exit selection mode' : 'Select tasks for bulk actions'}
            className={`inline-flex min-h-11 items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors ${
              s.selectionMode
                ? 'border-cyan-500/30 bg-cyan-500/10 text-cyan-300'
                : 'border-neutral-800/50 bg-neutral-900/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300'
            }`}
          >
            <CheckSquare className="h-3.5 w-3.5" />
            {s.selectionMode ? 'Selecting' : 'Select'}
          </button>
        )}
      </div>
    </div>
  );

  const pipelinePagination = (
    <Pagination page={s.page} onPageChange={s.setPage} taskCount={filteredTasks.length} serverTaskCount={s.tasks.length} limit={s.limit} onLimitChange={s.setLimit} totalTasks={s.totalTasks} />
  );
  if (s.authLoading) {
    return (
      <div className="h-screen flex items-center justify-center bg-neutral-950 text-neutral-400">
        <Activity className="w-5 h-5 animate-spin" />
      </div>
    );
  }

  if (!s.user) {
    return (
      <div className="h-screen flex items-center justify-center bg-neutral-950">
        <div className="bg-neutral-900 border border-neutral-800 rounded-xl p-8 text-center max-w-sm">
          <h1 className="text-lg font-semibold text-neutral-100 mb-2">Blueprint</h1>
          <p className="text-xs text-neutral-500 mb-6">Sign in with your Google account to continue</p>
          <button
            onClick={() => signInWithGoogle().catch(e => s.setError(String(e)))}
            className="bg-white hover:bg-neutral-200 text-neutral-950 px-6 py-2 rounded text-sm font-semibold transition-colors w-full"
          >
            Sign in with Google
          </button>
          {s.error && <p className="text-red-400 text-xs mt-4">{s.error}</p>}
        </div>
      </div>
    );
  }

  if (s.accessDenied) {
    return (
      <div className="h-screen flex items-center justify-center bg-neutral-950">
        <div className="bg-neutral-900 border border-neutral-800 rounded-xl p-8 text-center max-w-sm">
          <h1 className="text-lg font-semibold text-neutral-100 mb-2">Access Denied</h1>
          <p className="text-xs text-neutral-500 mb-4">
            Your account ({s.user.email}) is not authorized to access Blueprint.
          </p>
          <p className="text-xs text-neutral-600 mb-6">Contact an administrator to request access.</p>
          <button
            onClick={() => { void signOut(); }}
            className="bg-neutral-800 hover:bg-neutral-700 text-neutral-200 px-6 py-2 rounded text-sm font-medium transition-colors w-full"
          >
            Sign out
          </button>
        </div>
      </div>
    );
  }

  if (s.userRole === null) {
    return (
      <div className="h-screen flex items-center justify-center bg-neutral-950 text-neutral-400">
        <Activity className="w-5 h-5 animate-spin" />
      </div>
    );
  }

  const showWelcomeScreen = s.projectsLoaded && s.projects.length === 0 && !s.projectsFetchError;

  return (
    <ErrorBoundary>
    <TaskCapabilitiesProvider contract={s.taskCapabilityContract}>
    <div className="flex min-h-screen flex-col bg-[#08090b] text-neutral-200 font-sans md:h-screen md:overflow-hidden">
      {s.refreshing && (
        <div className="fixed top-0 left-0 right-0 z-50 h-0.5 bg-neutral-800 overflow-hidden">
          <div className="h-full w-2/5 bg-cyan-400" style={{ animation: 'progress-slide 1.2s ease-in-out infinite' }} />
        </div>
      )}
      <HeaderBar
        tasks={s.tasks}
        onRefresh={s.fetchTasks}
        showResearchBrowser={s.showResearchBrowser}
        onToggleResearchBrowser={() => s.setShowResearchBrowser(prev => !prev)}
        showKnowledgeBrowser={s.showKnowledgeBrowser}
        onToggleKnowledgeBrowser={() => s.setShowKnowledgeBrowser(prev => !prev)}
        onOpenActivityFeed={() => setShowActivityFeed(true)}
        openMessageCount={openMessageCount}
        newSinceLastVisitCount={newSinceLastVisit}
        needsAttentionCount={pipelineAttentionCount}
        onOpenNewTaskModal={s.openNewTaskModal}
        onOpenSettings={() => openRuntimeSettings('general')}
        onNewProject={() => setShowNewProjectModal(true)}
        onManageProjects={() => setShowProjectManagement(true)}
        onSignOut={() => { void signOut(); }}
        onSignOutAll={() => { void signOutAll(); }}
        onAddAccount={() => { addAccount().catch(e => s.setError(String(e))); }}
        onSwitchAccount={(id) => switchAccount(id)}
        accounts={getAccounts()}
        activeAccountId={getActiveAccountId()}
        userEmail={s.user.email || ''}
        wsRef={s.wsRef}
        apiHealthy={s.apiHealthy}
        wsConnected={s.wsConnected}
        lastFetchTime={s.lastFetchTime}
        deployState={s.deployState}
        selectedProjectId={s.selectedProjectId}
        projects={s.projects}
        onSelectProject={s.setSelectedProjectId}
        activeTab={activeTab}
        onTabChange={handleTabChange}
        onOpenCommandPalette={openCommandPalette}
        health={s.orchestratorHealth}
        detailedHealth={s.detailedHealth}
        pipelineThrottle={s.pipelineThrottle}
        ceilingDeferredProjectIds={s.ceilingDeferredProjectIds}
        gatedTaskTypes={s.gatedTaskTypes}
        costSectionAvailable={activeTab === 'runtime' && runtimeSection === 'analytics'}
        activeAlerts={activeAlerts}
      />
      <Suspense fallback={null}><CommandPaletteHost open={commandPaletteOpen} commands={workbenchCommands} query={commandPaletteQuery} onQueryChange={setCommandPaletteQuery} setOpen={setCommandPaletteOpen} /></Suspense>
      <Suspense fallback={null}><KeyboardShortcutsOverlay open={shortcutHelpOpen} onClose={() => setShortcutHelpOpen(false)} /></Suspense>
      <Suspense fallback={null}>
      <StrategicContextWorkspace
        open={strategicContextWorkspaceOpen}
        state={strategicReadModel.data ?? null}
        projectId={selectedProjectId}
        onClose={closeStrategicContextWorkspace}
        onOpenTarget={openStrategicContextWorkspaceTarget}
        onOpenMessageTarget={strategicMessageTarget.open}
      />
      </Suspense>
      {showProjectMembers && s.selectedProjectId && selectedProject && (() => {
        const projectId = s.selectedProjectId;
        return (
        <Suspense fallback={null}>
        <ProjectMembers
          projectId={projectId}
          members={s.projectMembers}
          myRole={selectedProject.my_role || 'viewer'}
          onAdd={(email, role) => s.addProjectMember(projectId, email, role)}
          onUpdateRole={(memberId, role) => s.updateProjectMemberRole(projectId, memberId, role)}
          onRemove={(memberId) => s.removeProjectMember(projectId, memberId)}
        />
        </Suspense>
        );
      })()}
      <VersionMismatchBanner />
      <Suspense fallback={null}>{s.userRole === 'admin' && <NullBranchDiagnosticsPanel onConfigure={handleConfigureBranchProject} />}</Suspense>
      {s.error && (
        <div className="mb-3 md:mb-6 bg-red-950/50 text-red-400 px-4 py-3 rounded-lg text-sm border border-red-900/50 flex items-center gap-2 shrink-0">
          <AlertCircle className="w-4 h-4" />
          <p>{s.error}</p>
        </div>
      )}

      {s.projectsFetchError && (
        <div className="mb-2 bg-yellow-950/50 text-yellow-400 px-4 py-2 rounded-lg text-sm border border-yellow-900/50 flex items-center gap-2 shrink-0">
          <AlertCircle className="w-4 h-4 shrink-0" />
          <p>{s.projectsFetchError}</p>
        </div>
      )}

      {showWelcomeScreen ? (
        <Suspense fallback={null}>
        <WelcomeScreen
          onCreateProject={() => setShowNewProjectModal(true)}
          onSignOut={() => { void signOut(); }}
        />
        </Suspense>
      ) : (
        <WorkbenchFrame>
      <BoardDataReadyReporter activeTab={activeTab} initialLoading={boardInitialLoading} refreshing={boardRefreshing} fetchEpoch={boardDataEpoch} />
      {s.showUserManagement && s.userRole === 'admin' ? (
        <Suspense fallback={null}>
        <UserManagement
          users={s.allowedUsers}
          currentUserEmail={s.user?.email || ''}
          onAdd={s.addAllowedUser}
          onUpdateRole={s.updateAllowedUserRole}
          onRemove={s.removeAllowedUser}
          onClose={() => s.setShowUserManagement(false)}
        />
        </Suspense>
      ) : activeTab === 'chat' ? (
        <WorkbenchSurface
          title="Strategic"
          showTitle={false}
          actions={(
            <>
              <StrategicSessionControl
                currentMode={strategicInteractionMode === 'continuous' ? 'continuous' : 'chat'}
                saving={savingStrategicWorkbench}
                sessionActive={strategicSessionActive}
                sessionStatus={strategicSessionStatus}
                sessionState={strategicSessionState}
                sessionReason={strategicSessionReason}
                sessionBusy={strategicSessionBusy}
                sessionRunners={residentStrategicSessionRunners}
                sessionSelectedRunnerId={strategicSessionSelectedRunnerId}
                onSelectRunner={handleSelectStrategicRunner}
                onStartSession={handleStartStrategicSession}
                onStopSession={handleStopStrategicSession}
                onModeChange={setStrategicInteractionMode}
                density="compact"
              />
              <button
                  type="button"
                  onClick={openStrategicContextWorkspace}
                  className="inline-flex min-h-11 shrink-0 items-center gap-1.5 rounded-md border border-neutral-800 bg-neutral-950 px-2.5 text-xs font-semibold text-neutral-300 transition-colors hover:border-neutral-700 hover:bg-neutral-900 hover:text-neutral-100 sm:min-h-9 lg:hidden"
                  title="Open Strategic context"
                  aria-label="Open Strategic context"
                >
                  <PanelRightOpen className="h-4 w-4 shrink-0 text-neutral-500" aria-hidden="true" />
                  <span>Context</span>
                </button>
              <button
                type="button"
                onClick={toggleChatRail}
                aria-pressed={!chatRailCollapsed}
                className={`hidden min-h-11 shrink-0 items-center gap-1.5 rounded-md border px-2.5 text-xs font-semibold transition-colors sm:min-h-9 lg:inline-flex ${
                  chatRailCollapsed
                    ? 'border-neutral-800 bg-neutral-950 text-neutral-300 hover:border-neutral-700 hover:bg-neutral-900 hover:text-neutral-100'
                    : 'border-neutral-700 bg-neutral-800/90 text-neutral-100'
                }`}
                title={chatRailCollapsed ? 'Show context panel' : 'Hide context panel'}
                aria-label={chatRailCollapsed ? 'Show context panel' : 'Hide context panel'}
              >
                {chatRailCollapsed
                  ? <PanelRightOpen className="h-4 w-4 shrink-0 text-neutral-500" aria-hidden="true" />
                  : <PanelRightClose className="h-4 w-4 shrink-0" aria-hidden="true" />}
                <span>Context</span>
              </button>
            </>
          )}
          flush
        >
          <WorkbenchSplit
            main={(
              <Suspense fallback={<WorkspaceSkeleton />}>
              <MessagesPanel
                selectedProjectId={s.selectedProjectId}
                githubRepo={selectedProject ? `${selectedProject.github_owner}/${selectedProject.github_repo}` : undefined}
                defaultBranch={s.promotionTargetData?.promotion_target || s.integrationBranchData?.integration_branch || undefined}
                variant="page"
                filterChrome="minimal"
                showCloseButton={false}
                addToast={s.addToast}
                onMessageCountChange={handleMessageCountChange}
                onOpenTask={handleViewTask}
                notifyOnHighSignalMessage={s.notifyOnHighSignalMessage}
                strategistActive={s.strategistActive}
                cycleProgress={s.cycleProgress}
                lastCycleCompletion={s.lastCycleCompletion}
                focusComposerSignal={strategicComposerFocusSignal}
                realtimeRefreshSignal={messageRealtimeRefreshSignal}
                realtimeCanonicalMessage={realtimeCanonicalProjection?.message ?? null}
                realtimeCanonicalRefetchRequired={realtimeCanonicalProjection?.canonicalRefetchRequired ?? false}
                strategicPollEpoch={strategicPollEpoch}
                targetMessage={strategicMessageTarget.target} onTargetMessageHandled={strategicMessageTarget.clear}
                strategicState={strategicReadModel.data ?? null}
                strategicWorkbenchState={strategicWorkbenchState}
                strategicSessionTranscript={s.strategicSessionTranscript}
                onStrategicInteraction={() => { persistStrategicWorkbench({ active_surface: 'strategic' }); }}
                onStrategicStateRefresh={() => { void strategicReadModel.refresh(); }}
                onOpenCommandPalette={openCommandPalette}
                onOpenStrategicContextWorkspace={openStrategicContextWorkspace}
                alerts={combinedAlerts}
                onDismissAlert={(id) => {
                  if (id.startsWith('zone-')) setDismissedZoneAlertIds(prev => new Set([...prev, id]));
                  else s.dismissAlert(id);
                }}
                onDismissAllAlerts={() => {
                  setDismissedZoneAlertIds(prev => new Set([...prev, ...zoneHealthAlerts.map(a => a.id)]));
                  s.dismissAllAlerts();
                }}
                onViewAlertTask={handleViewTask}
                onOpenActivityFeed={() => setShowActivityFeed(true)}
              />
              </Suspense>
            )}
            rail={(
              <Suspense fallback={<WorkspaceSkeleton />}>
              <StrategicContextRail
                projectId={s.selectedProjectId}
                state={strategicReadModel.data ?? null}
                loading={strategicReadModel.loading}
                error={strategicReadModel.error}
                strategistActive={s.strategistActive}
                cycleProgress={s.cycleProgress}
                lastCycleCompletion={s.lastCycleCompletion}
                onOpenProjectSection={(section) => openStrategicContextTarget({ surface: 'project', section })}
                onOpenPipeline={() => openStrategicContextTarget({ surface: 'pipeline', section: 'flow' })}
                onOpenRuntimeHealth={() => openStrategicContextTarget({ surface: 'runtime', section: 'health' })}
                onOpenRuntimeDevices={() => openStrategicContextTarget({ surface: 'runtime', section: 'devices' })}
                onOpenRuntimeCompute={() => openStrategicContextTarget({ surface: 'runtime', section: 'compute' })}
                onOpenAccounts={openRuntimeAccounts}
                onOpenSpend={openRuntimeAnalytics}
                onOpenContextWorkspace={openStrategicContextWorkspace}
                onOpenTask={handleViewTaskById}
                onOpenMessages={strategicMessageTarget.open}
                onOpenTarget={openStrategicContextTarget}
                accountSpend={strategicAccountSpend}
                onConfirmationResolved={() => { void strategicReadModel.refresh(); }}
                addToast={s.addToast}
              />
              </Suspense>
            )}
            railClassName="bg-workbench-rail"
            railCollapsed={chatRailCollapsed}
          />
        </WorkbenchSurface>
      ) : activeTab === 'project' ? (
        <ErrorBoundary level="panel">
          <Suspense fallback={<WorkspaceSkeleton />}>
          <ProjectWorkspace
            projectSection={projectSection}
            onProjectSectionChange={setProjectSection}
            projectManageSection={projectManageSection}
            onProjectManageSectionChange={setProjectManageSection}
            selectedProject={selectedProject ?? null}
            selectedProjectId={s.selectedProjectId}
            selectedProjectRepoLabel={selectedProjectRepoLabel}
            projects={s.projects}
            settingsRouterProps={settingsRouterProps}
            addToast={s.addToast}
            fetchDirectionsRef={s.fetchDirectionsRef}
            strategistActive={s.strategistActive}
            cycleProgress={s.cycleProgress}
            lastCycleCompletion={s.lastCycleCompletion}
            onOpenProjectManagement={() => setShowProjectManagement(true)}
            onOpenProjectMembers={() => setShowProjectMembers(true)}
            onViewFindings={(dirId) => { setResearchBrowserDirectionId(dirId); s.setShowResearchBrowser(true); }}
            onViewKnowledge={(dirId) => { setKnowledgeBrowserDirectionId(dirId); s.setShowKnowledgeBrowser(true); }}
            onNavigateToTask={handleViewTaskById}
          />
          </Suspense>
        </ErrorBoundary>
      ) : activeTab === 'runtime' ? (
        <ErrorBoundary level="panel">
          <Suspense fallback={<WorkspaceSkeleton />}>
          <RuntimeWorkspace
            runtimeSection={runtimeSection}
            onRuntimeSectionChange={setRuntimeSection}
            runtimeConfigSection={runtimeConfigSection}
            onRuntimeConfigSectionChange={setRuntimeConfigSection}
            selectedProjectId={s.selectedProjectId}
            selectedProjectRepoLabel={selectedProjectRepoLabel}
            projects={s.projects}
            tasks={s.tasks}
            scopedActiveCount={s.scopedActiveCount}
            scopedQueuedCount={s.scopedQueuedCount}
            globalActiveCount={s.globalActiveCount}
            globalQueuedCount={s.globalQueuedCount}
            userRole={s.userRole}
            apiHealthy={s.apiHealthy}
            wsConnected={s.wsConnected}
            settingsRouterProps={settingsRouterProps}
            onViewTask={handleOpenTaskDetails}
            onViewTaskById={handleViewTaskById}
            onOpenFleet={() => {
              s.setActiveTab('runtime');
              setRuntimeSection('devices');
            }}
            onCreateTask={s.openNewTaskModal}
            onLaunchCompute={() => {
              s.setComputeExperiment(defaultComputeExperimentForm());
              s.openPrefilledTaskModal({ taskType: 'compute_experiment', title: '', prompt: '' });
            }}
            onCloneExperiment={(form) => {
              s.setComputeExperiment(form);
              s.openPrefilledTaskModal({ taskType: 'compute_experiment', title: '', prompt: '' });
            }}
            onDirectionClick={(dirId) => { setResearchBrowserDirectionId(dirId); s.setShowResearchBrowser(true); }}
            onConfigureTargets={handleConfigureTargets}
            onSpawnOrphanFix={(pr) => s.openPrefilledTaskModal({ taskType: 'fix', title: pr.pr_title, prompt: `PR #${pr.pr_number} is open but has no active task - investigate and either close or push remediation. PR: ${pr.pr_url}`, prNumber: pr.pr_number, prBranch: pr.pr_branch })}
            gatedTaskTypes={s.gatedTaskTypes}
            onApproveExperiment={s.handleApprove}
            onDismissExperiment={s.handleDismiss}
            onNavigateToKnowledge={(docId) => { setKnowledgeBrowserInitialDocId(docId); s.setShowKnowledgeBrowser(true); }}
            onRestartOrchestrator={s.handleRestartOrchestrator}
            fleetRunnersRefreshSignal={s.fleetRunnersRefreshSignal}
            addToast={s.addToast}
            versionLifecycle={versionLifecycle}
            versionLifecycleError={versionLifecycleError}
            versionLifecycleLastSuccessAt={versionLifecycleLastSuccessAt}
            versionLifecycleRefreshing={versionLifecycleRefreshing}
            refreshVersionLifecycle={refreshVersionLifecycle}
          />
          </Suspense>
        </ErrorBoundary>
      ) : activeTab === 'fleet' ? (
        <ErrorBoundary level="panel">
          <Suspense fallback={<WorkspaceSkeleton />}>
          <FleetRunnersDashboard />
          </Suspense>
        </ErrorBoundary>
      ) : (
        <Suspense fallback={<WorkspaceSkeleton />}>
        <PipelineWorkspace
          surface={{
            pipelineSection,
            onPipelineSectionChange: setPipelineSection,
            showArchive: s.showArchive,
            setShowArchive: s.setShowArchive,
            viewMode: s.viewMode,
            setViewMode: s.setViewMode,
          }}
          chrome={{
            actions: pipelineSurfaceActions,
            filterStrip: pipelineFilterStrip,
            pagination: pipelinePagination,
          }}
          archive={{
            archiveTasks: s.archiveTasks,
            archiveLoading: s.archiveLoading,
            archivePage: s.archivePage,
            archivePageSize: s.archivePageSize,
            archiveStatusFilter: s.archiveStatusFilter,
            archiveSearchQuery: s.archiveSearchQuery,
            archiveDateRange: s.archiveDateRange,
            archiveTotalTasks: s.archiveTotalTasks,
            setArchivePage: s.setArchivePage,
            setArchivePageSize: s.setArchivePageSize,
            setArchiveStatusFilter: s.setArchiveStatusFilter,
            setArchiveSearchQuery: s.setArchiveSearchQuery,
            setArchiveDateRange: s.setArchiveDateRange,
            handleViewLogs: handleOpenTaskDetails,
            onRetryTask: s.handleRetry,
            onUnarchiveTask: s.handleUnarchiveTask,
            archiveSortOrder: s.archiveSortOrder,
            setArchiveSortOrder: s.setArchiveSortOrder,
            onPurgeArchive: s.handlePurgeArchive,
          }}
          flow={{
            tasks: filteredTasks,
            loading: s.initialLoading,
            hasActiveFilters,
            onViewLogs: handleOpenTaskDetails,
            onNavigateToParent: handleNavigateToParent,
            onClearWaveDeps: handleClearWaveDeps,
            onArchiveGroup: handleArchiveGroup,
            onUnarchiveTask: s.handleUnarchiveTask,
          }}
          rail={{
            currentTask: openPipelineTask,
            attentionTasks: backendAttentionTasks,
            localFallbackTasks: localAttentionTasks,
            trackedTaskIds: s.trackedTaskIds,
            onOpenTask: handleOpenTaskDetails,
            onOpenPipeline: () => handleTabChange('pipeline'),
            onTrackTask: s.trackTask,
          }}
          board={{
            tasks: filteredTasks,
            columns: visibleColumns,
            loading: s.initialLoading,
            maxRetries: s.maxRetries,
            actionLoadingMap: s.actionLoadingMap,
            compact: s.compactMode,
            sortBy,
            selectionMode: s.selectionMode,
            selectedTaskIds: s.selectedTaskIds,
            onToggleTaskSelection: s.toggleTaskSelection,
            onCtrlClick: handleBoardCtrlClick,
            onBulkToggle: s.bulkColumnToggle,
            onShiftSelect: s.shiftSelectRange,
            hasActiveFilters,
            onArchiveColumn: handleBoardArchiveColumn,
            onApprove: s.handleApprove,
            onRequestChanges: s.handleRequestChanges,
            onCancel: s.handleCancel,
            onRetry: s.handleRetry,
            onEditPrompt: s.handleEditPrompt,
            onDismiss: s.handleDismiss,
            onArchive: s.handleArchiveTask,
            onViewLogs: handleOpenTaskDetails,
            onTrack: s.trackTask,
            trackedTaskIds: s.trackedTaskIds,
            onDuplicate: s.openDuplicateTaskModal,
            searchQuery: s.searchQuery,
            onDirectionClick: handleBoardDirectionClick,
          }}
          metrics={{
            activeTasks: visibleActiveTasks,
            queuedTasks: visibleQueuedTasks,
            blockedTasks: visibleBlockedTasks,
            failedTasks: visibleFailedTasks,
            terminalTasks: visibleTerminalTasks,
          }}
          status={{
            selectedProjectId: s.selectedProjectId,
            selectedProjectLabel: selectedProjectRepoLabel,
            displayedRows: filteredTasks.length,
            loadedRows: s.tasks.length,
            serverTotalRows: s.totalTasks,
            lastFetchTime: s.lastFetchTime,
            apiHealthy: s.apiHealthy,
            wsConnected: s.wsConnected,
          }}
          stream={{
            trackedTaskIds: s.trackedTaskIds,
            trackedLogs: s.trackedLogs,
            tasks: s.tasks,
            onUntrack: s.untrackTask,
            onUntrackAll: s.untrackAll,
          }}
        />
        </Suspense>
      )}
      </WorkbenchFrame>
      )}

      <PrimaryTabNav
        activeTab={activeTab}
        onTabChange={(tab) => {
          if (tab === 'pipeline' && s.trackedTaskIds.size > 0) {
            openTrackedTaskStreams();
            return;
          }
          handleTabChange(tab);
        }}
        openMessageCount={openMessageCount}
        newSinceLastVisitCount={newSinceLastVisit}
        needsAttentionCount={pipelineAttentionCount}
        activeAlerts={activeAlerts}
        placement="mobile"
        className="md:hidden"
      />

      <WorkbenchStatusBar
        left={(
          <>
            <span>{selectedProject ? `${selectedProject.github_owner}/${selectedProject.github_repo}` : 'No project selected'}</span>
            <span>{activeTab}</span>
            <span>{s.wsConnected ? 'ws connected' : 'ws offline'}</span>
          </>
        )}
        right={(
          <>
            {s.trackedTaskIds.size > 0 && (
              <button
                type="button"
                data-viewport-allow-small-target
                onClick={openTrackedTaskStreams}
                className="rounded border border-neutral-800 bg-neutral-950/70 px-1.5 text-neutral-400 transition-colors hover:border-neutral-700 hover:text-neutral-100"
                title="Open tracked task streams in Pipeline"
              >
                tracking {s.trackedTaskIds.size}
              </button>
            )}
            <span>{s.globalActiveCount} active</span>
            <span>{s.globalQueuedCount} queued</span>
            <span>${s.cumulativeCost.toFixed(2)}</span>
          </>
        )}
      />

      {s.showNewTaskModal && (
        <Suspense fallback={null}>
        <NewTaskModal
          newTaskRepo={s.newTaskRepo}
          newTaskBranch={s.newTaskBranch}
          newTaskTitle={s.newTaskTitle}
          newTaskPrompt={s.newTaskPrompt}
          newTaskType={s.newTaskType}
          newTaskPrNumber={s.newTaskPrNumber}
          newTaskPrBranch={s.newTaskPrBranch}
          setNewTaskRepo={s.setNewTaskRepo}
          setNewTaskBranch={s.setNewTaskBranch}
          setNewTaskTitle={s.setNewTaskTitle}
          setNewTaskPrompt={s.setNewTaskPrompt}
          setNewTaskType={s.setNewTaskType}
          setNewTaskPrNumber={s.setNewTaskPrNumber}
          setNewTaskPrBranch={s.setNewTaskPrBranch}
          newTaskModel={s.newTaskModel}
          newTaskEffort={s.newTaskEffort}
          setNewTaskModel={s.setNewTaskModel}
          setNewTaskEffort={s.setNewTaskEffort}
          computeExperiment={s.computeExperiment}
          setComputeExperiment={s.setComputeExperiment}
          getDefaultModelForType={s.getDefaultModelForType}
          getDefaultEffortForType={s.getDefaultEffortForType}
          preflightResult={s.preflightResult}
          preflightLoading={s.preflightLoading}
          preflightError={s.preflightError}
          ideaText={s.ideaText}
          setIdeaText={s.setIdeaText}
          draftNotes={s.draftNotes}
          setDraftNotes={s.setDraftNotes}
          onDraftApplied={s.onDraftApplied}
          repoOptions={s.repoOptions}
          repoLoading={s.repoLoading}
          repoError={s.repoError}
          useManualRepo={s.useManualRepo}
          setUseManualRepo={s.setUseManualRepo}
          branchOptions={s.branchOptions}
          branchLoading={s.branchLoading}
          branchError={s.branchError}
          useManualBranch={s.useManualBranch}
          setUseManualBranch={s.setUseManualBranch}
          setBranchOptions={s.setBranchOptions}
          launchableTaskTypeOptions={s.launchableTaskTypeOptions}
          taskBranchDefaultsReady={s.taskBranchDefaultsReady}
          taskBranchDefaultsLoading={s.taskBranchDefaultsLoading}
          taskBranchDefaultsError={s.taskBranchDefaultsError}
          integrationBranch={s.integrationBranchData?.integration_branch}
          onRepoChange={s.handleRepoChange}
          onSubmit={s.handleCreateTask}
          onClose={() => s.setShowNewTaskModal(false)}
          fleetRunners={s.fleetRunners}
          fleetRunnersLoading={s.fleetRunnersLoading}
        />
        </Suspense>
      )}

      {(showNewProjectModal || setupProjectId) && (
        <Suspense fallback={null}>
        <NewProjectModal
          onClose={() => { setShowNewProjectModal(false); setSetupProjectId(null); }}
          onSuccess={() => {
            // Refresh project list but keep modal open so the setup guide is visible
            s.fetchProjects();
          }}
          setupProject={setupProject}
        />
        </Suspense>
      )}

      {showProjectManagement && (
        <Suspense fallback={null}>
        <ProjectManagementModal
          projects={s.projects}
          selectedProjectId={s.selectedProjectId}
          onNewProject={() => setShowNewProjectModal(true)}
          onRefreshProjects={s.fetchProjects}
          onSelectProject={s.setSelectedProjectId}
          onClose={() => setShowProjectManagement(false)}
        />
        </Suspense>
      )}

      {s.showResearchBrowser && (
        <Suspense fallback={<WorkspaceSkeleton />}>
          <ResearchBrowser
            tasks={s.tasks}
            researchFeed={researchFeed.length > 0 ? researchFeed : undefined}
            selectedProjectId={s.selectedProjectId}
            initialDirectionId={researchBrowserDirectionId ?? undefined}
            onClose={() => { s.setShowResearchBrowser(false); setResearchBrowserDirectionId(null); }}
            onNavigateToTask={(taskId) => {
              s.setShowResearchBrowser(false);
              setResearchBrowserDirectionId(null);
              handleViewTaskById(taskId);
            }}
          />
        </Suspense>
      )}

      {s.showKnowledgeBrowser && (
        <Suspense fallback={<WorkspaceSkeleton />}>
          <KnowledgeBrowser
            selectedProjectId={s.selectedProjectId}
            onClose={() => { s.setShowKnowledgeBrowser(false); setKnowledgeBrowserDirectionId(null); setKnowledgeBrowserInitialDocId(null); }}
            onLaunchWebResearchTask={s.openWebResearchTaskModal}
            initialDirectionId={knowledgeBrowserDirectionId ?? undefined}
            initialDocumentId={knowledgeBrowserInitialDocId ?? undefined}
            onNavigateToTask={(taskId) => {
              s.setShowKnowledgeBrowser(false);
              handleViewTaskById(taskId);
            }}
          />
        </Suspense>
      )}

      {s.logTask && (
        <ErrorBoundary level="panel">
        <Suspense fallback={null}>
        <TaskDetailDrawer
          task={s.logTask}
          tasks={s.tasks}
          logLines={s.logLines}
          logsConnecting={s.logsConnecting}
          wsDisconnected={s.wsDisconnected}
          wsDisconnectReason={s.wsDisconnectReason}
          actionLoading={s.actionLoadingMap[s.logTask.id]}
          gcpProjectId={s.deployState?.gcp_project_id}
          onClose={s.closeLogViewer}
          onApprove={s.handleApprove}
          onRequestChanges={s.handleRequestChanges}
          onCancel={s.handleCancel}
          onRetry={s.handleRetry}
          onEditPrompt={s.handleEditPrompt}
          onDismiss={s.handleDismiss}
          onDuplicate={s.openDuplicateTaskModal}
          onResetIterationCap={s.handleResetIterationCap}
          onTriggerRollup={s.handleTriggerRollup}
          onNavigateToTask={(taskId) => {
            handleViewTaskById(taskId);
          }}
        />
        </Suspense>
        </ErrorBoundary>
      )}

      <Suspense fallback={null}>
      {s.selectionMode && s.selectedTaskIds.size > 0 && (
        <BulkActionBar
          selectedCount={s.selectedTaskIds.size}
          approvableTasks={approvableTasks}
          cancellableTasks={cancellableTasks}
          failedTasks={failedTasks}
          terminalTasks={terminalTasks}
          setConfirmDialog={s.setConfirmDialog}
          handleBulkAction={s.handleBulkAction}
          handleBulkCancel={s.handleBulkCancel}
          handleBulkArchiveColumn={s.handleBulkArchiveColumn}
          toggleSelectionMode={s.toggleSelectionMode}
        />
      )}
      </Suspense>

      {showActivityFeed && (
        <Suspense fallback={null}>
        <ActivityFeed
          selectedProjectId={s.selectedProjectId}
          refreshInterval={s.refreshInterval}
          onSelectTask={(task) => {
            setShowActivityFeed(false);
            handleViewTask(task.id);
          }}
          onClose={() => setShowActivityFeed(false)}
          lastVisitTimestamp={s.lastVisitTimestamp}
        />
        </Suspense>
      )}

      <ToastContainer toasts={s.toasts} onDismiss={s.dismissToast} />

      {s.showFleetDetail && (
        <Suspense fallback={null}>
        <FleetDetailDrawer
          onClose={() => s.setShowFleetDetail(false)}
          onViewTask={handleViewTask}
          projects={s.projects}
          selectedProjectId={s.selectedProjectId}
          fleetRunnersRefreshSignal={s.fleetRunnersRefreshSignal}
          live={s.wsConnected}
        />
        </Suspense>
      )}

      {s.confirmDialog && (
        <ConfirmDialog
          title={s.confirmDialog.title}
          message={s.confirmDialog.message}
          confirmLabel={s.confirmDialog.confirmLabel}
          onConfirm={s.confirmDialog.onConfirm}
          onCancel={() => s.setConfirmDialog(null)}
        />
      )}
    </div>
    </TaskCapabilitiesProvider>
    </ErrorBoundary>
  );
}
