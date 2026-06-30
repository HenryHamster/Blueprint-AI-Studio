import { useMemo, useRef, useEffect, useState, useCallback } from 'react';
import { ErrorBoundary } from './components/ErrorBoundary';
import { Activity, AlertCircle, CheckSquare, LayoutList } from 'lucide-react';
import { ColumnsPicker } from './components/ColumnsPicker';
import type { ColumnPref } from './components/ColumnsPicker';
import { ConfirmDialog } from './components/ConfirmDialog';
import { useAppState } from './useAppState';
import { matchesTaskTypeFilter, resolveTaskCapabilities, TaskCapabilitiesProvider } from './taskCapabilities';
import { usePoller, DEFAULT_POLL_MS } from './hooks/usePoller';
import TaskStreamPanel from './components/TaskStreamPanel';
import { ResearchBrowser } from './components/ResearchBrowser';
import { KnowledgeBrowser } from './components/KnowledgeBrowser';
import { PipelineView } from './components/PipelineView';
import type { StrategicCockpitDirection } from './components/StrategicCockpit';
import { auth, signInWithGoogle, signOut } from './firebase';
import { fetchTask, apiFetch, API_BASE_URL } from './api';
import { countHighSignalMessages, countNewSinceLastVisit, loadLastSeen, fetchBackendLastSeen, resolveLastSeen, loadEventPrefs, loadTemporaryMutes, fetchServerNoisePrefs, fetchServerTempMutes, resolveNoisePrefs, resolveServerTempMutes, type EventPref, type TemporaryMutes } from './components/MessagesPanel.utils';
import { type SrInvestigationSignal } from './components/SrTrendBreakdown';
import { TaskDetailDrawer } from './components/TaskDetailDrawer';
import { ToastContainer } from './components/Toasts';
import { AlertsPopover } from './components/AlertsPopover';
import { FleetDetailDrawer } from './components/FleetDetailDrawer';
import { NewTaskModal } from './components/NewTaskModal';
import { NewProjectModal } from './components/NewProjectModal';
import { ProjectManagementModal } from './components/ProjectManagementModal';
import { HeaderBar } from './components/HeaderBar';
import type { SectionId } from './components/SettingsDropdown';
import { SearchFilterBar, SingleSelectDropdown } from './components/SearchFilterBar';
import type { SettingsSectionRouterProps } from './components/SettingsSectionRouter';
import type { PrimaryTab } from './hooks/useUIState';
import { defaultComputeExperimentForm } from './hooks/useTaskModal';
import { BulkActionBar } from './components/BulkActionBar';
import { KanbanBoard } from './components/KanbanBoard';
import type { SortBy } from './components/KanbanBoard';
import { ACTIVE_PIPELINE_STATUSES, CANCELLABLE_STATUSES, COLUMNS, SORT_KEY, TERMINAL_STATUSES } from './appWorkbenchConstants';
import { downloadTaskCsv, orderTasksForKanbanExport } from './utils/taskCsv';
import { Pagination } from './components/Pagination';
import ArchiveView from './components/ArchiveView';
import { UserManagement } from './components/UserManagement';
import { ProjectMembers } from './components/ProjectMembers';
import ActivityFeed from './components/ActivityFeed';
import { MessagesPanel } from './components/MessagesPanel';
import { StrategicContextRail } from './components/StrategicContextRail';
import { StrategicContextWorkspace } from './components/StrategicContextWorkspace';
import { StrategicSessionControl, type StrategicInteractionMode } from './components/StrategicSessionControl';
import { TaskContextRail } from './components/TaskContextRail';
import { CommandPaletteHost } from './components/CommandPaletteHost';
import { QuickFilterPresets } from './components/QuickFilterPresets';
import { BoardSavedViewsBar, type BoardFilterSnapshot } from './components/BoardSavedViewsBar';
import { FAILURE_CLASS_CONFIG } from './components/TaskCard';
import type { Task } from './components/TaskCard';
import { VersionMismatchBanner } from './components/VersionMismatchBanner';
import { NullBranchDiagnosticsPanel } from './components/NullBranchDiagnosticsPanel';
import { ProjectWorkspace, RuntimeWorkspace, type ProjectManageSection, type ProjectSection, type RuntimeSection, type RuntimeSettingsSection } from './components/OperatorWorkspaces';
import { WorkbenchFrame, WorkbenchMetric, WorkbenchPanel, WorkbenchSplit, WorkbenchStatusBar, WorkbenchSurface } from './components/WorkbenchLayout';
import { useStrategicReadModel } from './hooks/useStrategicReadModel';
import { useOperatorKeyboardShortcuts } from './hooks/useOperatorKeyboardShortcuts';
import { useStrategicMessageTarget } from './hooks/useStrategicMessageTarget';
import { useCommandPaletteState } from './hooks/useCommandPaletteState';
import { useStrategicContextNavigation } from './hooks/useStrategicContextNavigation';
import { useWorkbenchCommands, type PipelineSection } from './hooks/useWorkbenchCommands';
import { updateStrategicWorkbenchState, type StrategicWorkbenchState } from './strategicStateApi';
import { parseTimestamp } from './utils/time';
import { matchesStatusFilter } from './utils/statusFilter';
import { buildOptimisticStrategicWorkbench, normalizeStrategicInteractionMode, tabForWorkbenchSurface, workbenchSurfaceForTab } from './utils/strategicWorkbenchSession';

type StrategicSessionRefs = StrategicWorkbenchState['selected_refs'];
export default function App() {
  const refreshStrategicReadModelRef = useRef<() => void>(() => {});
  const s = useAppState({ refreshStrategicReadModelRef });
  const searchInputRef = useRef<HTMLInputElement>(null);
  const [drawerWidth, setDrawerWidth] = useState(0);
  // Dedup guard: track the last lastCycleCompletion object we toasted for so a
  // WS reconnect that re-emits the same event doesn't fire a second toast.
  const lastCycleCompletionToastRef = useRef<typeof s.lastCycleCompletion>(null);
  const { lastCycleCompletion: cycleCompletion, addToast } = s;
  useEffect(() => {
    if (!cycleCompletion) return;
    if (cycleCompletion === lastCycleCompletionToastRef.current) return;
    lastCycleCompletionToastRef.current = cycleCompletion;
    addToast('Strategic cycle complete', 'success');
  }, [cycleCompletion, addToast]);
  const [openMessageCount, setOpenMessageCount] = useState(0);
  const [newSinceLastVisit, setNewSinceLastVisit] = useState(0);
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
  // Apply a saved view's snapshot through the live filter/sort setters so the
  // active-filter persistence layer keeps working exactly as before. Destructure
  // the (stable) setters so exhaustive-deps can track them without depending on
  // the whole per-render state object `s`.
  const {
    setSearchQuery, setTaskTypeFilter, setStatusFilter, setDateRange,
    setBlockedFilter, setRequiresApproval, setStuckFilter, setStuckMinSeconds,
    setFailureClassFilter,
  } = s;
  useEffect(() => {
    const onFailureClassFilter = (e: Event) => {
      const failureClass = (e as CustomEvent<string>).detail;
      if (failureClass && failureClass in FAILURE_CLASS_CONFIG) setFailureClassFilter([failureClass]);
    };
    window.addEventListener('blueprint:failure-class-filter', onFailureClassFilter);
    return () => window.removeEventListener('blueprint:failure-class-filter', onFailureClassFilter);
  }, [setFailureClassFilter]);
  useEffect(() => {
    localStorage.setItem(SORT_KEY, sortBy);
  }, [sortBy]);
  const resolvedTaskCapabilities = useMemo(
    () => resolveTaskCapabilities(s.taskCapabilityContract),
    [s.taskCapabilityContract],
  );
  const filterTaskTypeIds = resolvedTaskCapabilities.filterIds;
  // Snapshot of the full task-board filter combination + sort, fed to the saved-views
  // switcher so it can detect "modified" state and capture new views.
  const boardFilterSnapshot: BoardFilterSnapshot = {
    query: s.searchQuery,
    taskTypeFilter: s.taskTypeFilter,
    statusFilter: s.statusFilter,
    dateRange: s.dateRange,
    blocked: s.blockedFilter,
    requiresApproval: s.requiresApproval,
    stuck: s.stuckFilter,
    stuckMinSeconds: s.stuckMinSeconds,
    failureClassFilter: s.failureClassFilter,
    sortBy,
  };
  const applyBoardView = useCallback((snap: BoardFilterSnapshot) => {
    setSearchQuery(snap.query);
    setTaskTypeFilter(snap.taskTypeFilter);
    setStatusFilter(snap.statusFilter);
    setDateRange(snap.dateRange);
    setBlockedFilter(snap.blocked);
    setRequiresApproval(snap.requiresApproval);
    setStuckFilter(snap.stuck);
    setStuckMinSeconds(snap.stuckMinSeconds);
    setFailureClassFilter(snap.failureClassFilter);
    setSortBy(snap.sortBy);
  }, [setSearchQuery, setTaskTypeFilter, setStatusFilter, setDateRange, setBlockedFilter, setRequiresApproval, setStuckFilter, setStuckMinSeconds, setFailureClassFilter]);
  // Update browser tab title with unread message count
  useEffect(() => {
    if (openMessageCount > 0) {
      document.title = `(${openMessageCount}) Blueprint AI Studio`;
    } else {
      document.title = 'Blueprint AI Studio';
    }
  }, [openMessageCount]);
  // Fetch open message count periodically (when panel is closed the callback won't fire)
  const selectedProjectId = s.selectedProjectId;
  const activeTab: PrimaryTab = s.activeTab;
  const messagesPanelOpen = activeTab === 'chat';
  const [directions, setDirections] = useState<StrategicCockpitDirection[]>([]);
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
      const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(pid)}/directions`, undefined, 2, 30000, signal);
      if (signal.aborted) return;
      if (!res.ok) {
        return;
      }
      const data: unknown = await res.json();
      if (signal.aborted || pid !== selectedProjectId) return;
      const list = Array.isArray(data) ? data : ((data as Record<string, unknown>)?.directions ?? []);
      setDirections((list as Array<Record<string, unknown>>)
        .map((d): StrategicCockpitDirection | null => {
          const id = typeof d.id === 'string' ? d.id : '';
          if (!id) return null;
          const text = typeof d.text === 'string'
            ? d.text
            : typeof d.title === 'string'
              ? d.title
              : id;
          return {
            id,
            text,
            priority: typeof d.priority === 'string' ? d.priority : null,
            status: typeof d.status === 'string' ? d.status : null,
            category: typeof d.category === 'string' ? d.category : null,
            last_researched_at: typeof d.last_researched_at === 'string' ? d.last_researched_at : null,
            held: d.held === true,
          };
        })
        .filter((d): d is StrategicCockpitDirection => d !== null));
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
      const params = new URLSearchParams({ project_id: pid, status: 'open', limit: '100' });
      const [res, backendTs] = await Promise.all([
        apiFetch(`${API_BASE_URL}/api/admin/messages?${params}`),
        fetchBackendLastSeen(pid),
      ]);
      if (!res.ok || signal.aborted) return;
      const data = await res.json();
      const msgs: Array<{ message_type: string; from_type: string; status: string; context: Record<string, unknown>; created_at?: string | null }> = data.messages ?? [];
      if (!signal.aborted) {
        const noisePrefs = closedPanelNoisePrefsRef.current;
        const tempMutes = closedPanelTempMutesRef.current;
        setOpenMessageCount(countHighSignalMessages(msgs, noisePrefs, tempMutes));
        const localTs = loadLastSeen(s.user?.email, pid);
        const lastSeenTs = resolveLastSeen(localTs, backendTs);
        setNewSinceLastVisit(countNewSinceLastVisit(msgs, lastSeenTs, noisePrefs, tempMutes));
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
  const selectedProjectRepoLabel = selectedProject
    ? `${selectedProject.github_owner}/${selectedProject.github_repo}`
    : null;
  // Fetch project members when panel opens or project changes
  const { fetchProjectMembers } = s;
  useEffect(() => {
    if (showProjectMembers && selectedProjectId) {
      fetchProjectMembers(selectedProjectId);
    }
  }, [showProjectMembers, selectedProjectId, fetchProjectMembers]);
  const handleViewTask = (taskId: string) => {
    const task = s.tasks.find(t => t.id === taskId);
    if (task) s.handleViewLogs(task);
    else fetchTask(taskId).then(s.handleViewLogs).catch(() => {});
  };

  const [dismissedZoneAlertIds, setDismissedZoneAlertIds] = useState<Set<string>>(new Set());
  const [researchBrowserDirectionId, setResearchBrowserDirectionId] = useState<string | null>(null);
  const [knowledgeBrowserDirectionId, setKnowledgeBrowserDirectionId] = useState<string | null>(null);
  const [knowledgeBrowserInitialDocId, setKnowledgeBrowserInitialDocId] = useState<string | null>(null);
  const [researchFeed, setResearchFeed] = useState<Task[]>([]);
  // Fetch project tasks when the Findings Journal opens so findings and implementation child
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
  // Open operator-spawned SR investigations, keyed by category — used to dedup spawning.
  const [srInvestigationTaskIds, setSrInvestigationTaskIds] = useState<Record<string, string>>({});
  useEffect(() => {
    if (!selectedProjectId) setSrInvestigationTaskIds({});
  }, [selectedProjectId]);
  usePoller(async (signal) => {
    if (!selectedProjectId) return;
    const pid = selectedProjectId;
    try {
      const res = await apiFetch(`${API_BASE_URL}/api/admin/projects/${encodeURIComponent(pid)}/sr-investigation-task-ids`);
      if (!res.ok || signal.aborted) return;
      const data = (await res.json()) as { task_ids?: Record<string, string> };
      if (!signal.aborted) setSrInvestigationTaskIds(data.task_ids ?? {});
    } catch { /* ignore */ }
  }, { intervalMs: DEFAULT_POLL_MS, enabled: !!selectedProjectId, deps: [selectedProjectId] });

  const handleSpawnSrInvestigation = useCallback(async (signal: SrInvestigationSignal): Promise<string | null> => {
    if (!selectedProjectId) return null;
    try {
      const res = await apiFetch(`${API_BASE_URL}/api/admin/projects/${encodeURIComponent(selectedProjectId)}/sr-investigation`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(signal),
      });
      if (!res.ok) return null;
      const data = (await res.json()) as { task_id: string; created: boolean };
      setSrInvestigationTaskIds(prev => ({ ...prev, [signal.category]: data.task_id }));
      return data.task_id;
    } catch {
      return null;
    }
  }, [selectedProjectId]);

  const [settingsInitialSection, setSettingsInitialSection] = useState<SectionId | null>(null);

  const handleConfigureTargets = useCallback(() => {
    setSettingsInitialSection('build-config');
    s.handleToggleSettings();
    setTimeout(() => {
      const element = document.getElementById('sr-catch-rate-targets');
      element?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, 100);
  }, [s]);

  const handleConfigureBranchProject = useCallback((projectId: string) => {
    s.setSelectedProjectId(projectId);
    setSettingsInitialSection('branches');
    if (!s.showSettings) s.handleToggleSettings();
  }, [s]);

  // Clear the deep-link section once Settings closes, so a subsequent plain
  // Settings open is not forced back to the build-config tab.
  useEffect(() => {
    if (!s.showSettings) setSettingsInitialSection(null);
  }, [s.showSettings]);

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
      });
    }
    return alerts.filter(a => !dismissedZoneAlertIds.has(a.id));
  }, [s.zoneHealth, dismissedZoneAlertIds]);

  const combinedAlerts = useMemo(() => [...zoneHealthAlerts, ...s.alerts], [zoneHealthAlerts, s.alerts]);
  const alertMaxSeverity: 'ERROR' | 'WARNING' | 'INFO' | null = combinedAlerts.some(a => a.severity === 'ERROR') ? 'ERROR'
    : combinedAlerts.some(a => a.severity === 'WARNING') ? 'WARNING'
    : combinedAlerts.length > 0 ? 'INFO' : null;

  const runningTasks = useMemo(() => s.tasks.filter(t => t.status === 'IN_PROGRESS' || t.status === 'PROVISIONING'), [s.tasks]);
  const queuedTasks = useMemo(() => s.tasks.filter(t => t.status === 'QUEUED'), [s.tasks]);
  const strategicReadModel = useStrategicReadModel(s.selectedProjectId);
  useEffect(() => {
    refreshStrategicReadModelRef.current = () => {
      void strategicReadModel.refresh();
    };
    return () => {
      refreshStrategicReadModelRef.current = () => {};
    };
  }, [strategicReadModel.refresh]);
  const [optimisticStrategicWorkbench, setOptimisticStrategicWorkbench] = useState<StrategicWorkbenchState | null>(null);
  const [savingStrategicWorkbench, setSavingStrategicWorkbench] = useState(false);
  const strategicWorkbenchStateRef = useRef<StrategicWorkbenchState | null>(null);
  const strategicWorkbenchSaveSeqRef = useRef(0);
  const strategicWorkbenchSaveQueueRef = useRef<Promise<void>>(Promise.resolve());
  const strategicWorkbenchOperatorSurfaceTouchedRef = useRef(false);
  const strategicWorkbenchRestoreProjectRef = useRef<string | null>(null);
  useEffect(() => {
    strategicWorkbenchStateRef.current = null;
    setOptimisticStrategicWorkbench(null);
    setSavingStrategicWorkbench(false);
    strategicWorkbenchSaveSeqRef.current += 1;
    strategicWorkbenchSaveQueueRef.current = Promise.resolve();
    strategicWorkbenchOperatorSurfaceTouchedRef.current = false;
    strategicWorkbenchRestoreProjectRef.current = null;
  }, [selectedProjectId]);
  const strategicReadModelWorkbench = strategicReadModel.data?.project_id === selectedProjectId
    ? strategicReadModel.data?.workbench ?? null
    : null;
  const strategicWorkbenchState = optimisticStrategicWorkbench ?? strategicReadModelWorkbench;
  useEffect(() => {
    strategicWorkbenchStateRef.current = strategicWorkbenchState;
  }, [strategicWorkbenchState]);
  const persistStrategicWorkbench = useCallback((patch: Partial<StrategicWorkbenchState>) => {
    const projectId = s.selectedProjectId;
    if (!projectId) return;
    const saveSeq = strategicWorkbenchSaveSeqRef.current + 1;
    strategicWorkbenchSaveSeqRef.current = saveSeq;
    const base = strategicWorkbenchStateRef.current ?? strategicReadModelWorkbench;
    const optimistic = buildOptimisticStrategicWorkbench(base, patch);
    strategicWorkbenchStateRef.current = optimistic;
    setOptimisticStrategicWorkbench(optimistic);
    setSavingStrategicWorkbench(true);

    const save = strategicWorkbenchSaveQueueRef.current
      .catch(() => undefined)
      .then(async () => {
        try {
          const saved = await updateStrategicWorkbenchState(projectId, patch);
          if (strategicWorkbenchSaveSeqRef.current !== saveSeq) return;
          strategicWorkbenchStateRef.current = saved;
          setOptimisticStrategicWorkbench(saved);
          void strategicReadModel.refresh();
        } catch {
          if (strategicWorkbenchSaveSeqRef.current === saveSeq) {
            strategicWorkbenchStateRef.current = base;
            setOptimisticStrategicWorkbench(base);
            s.addToast('Could not save Strategic session', 'error');
          }
        } finally {
          if (strategicWorkbenchSaveSeqRef.current === saveSeq) setSavingStrategicWorkbench(false);
        }
      });
    strategicWorkbenchSaveQueueRef.current = save.then(
      () => undefined,
      () => undefined,
    );
  }, [s, strategicReadModel, strategicReadModelWorkbench]);
  const updateStrategicSessionRefs = useCallback((updateRefs: (refs: StrategicSessionRefs) => StrategicSessionRefs) => {
    const baseRefs = (strategicWorkbenchStateRef.current ?? strategicReadModelWorkbench)?.selected_refs ?? [];
    persistStrategicWorkbench({ selected_refs: updateRefs(baseRefs) });
  }, [persistStrategicWorkbench, strategicReadModelWorkbench]);
  const pipelineAttentionCount = useMemo(() => {
    const counts = strategicReadModel.data?.attention?.counts;
    if (!counts) return s.needsAttentionCount;
    return (
      (counts.approvals ?? 0) +
      (counts.failed_tasks ?? 0) +
      (counts.blocked_tasks ?? 0) +
      (counts.parked_tasks ?? 0)
    );
  }, [s.needsAttentionCount, strategicReadModel.data?.attention?.counts]);
  const { open: commandPaletteOpen, query: commandPaletteQuery, setOpen: setCommandPaletteOpen, setQuery: setCommandPaletteQuery } = useCommandPaletteState();
  const [strategicContextWorkspaceOpen, setStrategicContextWorkspaceOpen] = useState(false);
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

  const visibleColumns = useMemo(() => {
    return effectiveColumnPrefs
      .filter(pref => pref.visible)
      .map(pref => COLUMNS.find(c => c.id === pref.id))
      .filter((c): c is typeof COLUMNS[number] => !!c)
      .filter(col => col.includes.some(status => s.statusFilter.includes(status)));
  }, [effectiveColumnPrefs, s.statusFilter]);

  const stuckTaskIds = useMemo(() => new Set((s.diagnostics?.stuck_tasks ?? []).filter((st: { task_id: string; stuck_for_seconds: number }) => st.stuck_for_seconds >= s.stuckMinSeconds).map((st: { task_id: string }) => st.task_id)), [s.diagnostics, s.stuckMinSeconds]);
  const stuckCount1h = useMemo(() => (s.diagnostics?.stuck_tasks ?? []).filter((st: { stuck_for_seconds: number }) => st.stuck_for_seconds >= 3600).length, [s.diagnostics]);

  const dateRangeCutoffs: Record<string, number> = { '1h': 3600000, '6h': 21600000, '24h': 86400000, '7d': 604800000 };
  const filteredTasks = useMemo(() => {
    const now = new Date().getTime();
    const cutoff = s.dateRange !== 'all' ? now - dateRangeCutoffs[s.dateRange] : null;
    return s.tasks.filter(t => {
      const taskType = t.task_type || (t.payloads?.[0] as Record<string, unknown>)?.type as string || 'create';
      const typeMatch = matchesTaskTypeFilter(taskType, s.taskTypeFilter, filterTaskTypeIds);
      const statusMatch = matchesStatusFilter(t.status, s.statusFilter);
      const query = s.searchQuery.toLowerCase();
      const searchMatch = !query || t.id.toLowerCase().includes(query) || (t.title?.toLowerCase().includes(query)) || (t.description?.toLowerCase().includes(query)) || (t.latest_status_message?.toLowerCase().includes(query));
      const projectMatch = s.selectedProjectId === null || t.project_id === s.selectedProjectId;
      const dateMatch = cutoff === null || parseTimestamp(t.created_at).getTime() >= cutoff;
      // Prefer the first-class BLOCKED status; fall back to the legacy
      // QUEUED+depends_on inference for older payloads predating the status.
      const blockedMatch = !s.blockedFilter || t.status === 'BLOCKED' || (t.status === 'QUEUED' && t.depends_on != null && t.depends_on.length > 0);
      const approvalMatch = !s.requiresApproval || t.requires_approval;
      const failureClassMatch = s.failureClassFilter.length === 0 || (t.status === 'FAILED' && t.failure_class != null && s.failureClassFilter.includes(t.failure_class));
      const stuckMatch = !s.stuckFilter || stuckTaskIds.has(t.id);
      return typeMatch && statusMatch && searchMatch && projectMatch && dateMatch && blockedMatch && approvalMatch && failureClassMatch && stuckMatch;
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [s.tasks, s.taskTypeFilter, s.statusFilter, s.searchQuery, s.selectedProjectId, s.dateRange, s.blockedFilter, s.requiresApproval, s.stuckFilter, s.stuckMinSeconds, stuckTaskIds, s.failureClassFilter, filterTaskTypeIds]);

  const orderedExportTasks = useMemo(() => {
    if (s.viewMode !== 'kanban') return filteredTasks;
    if (visibleColumns.length === 0) return filteredTasks;
    return orderTasksForKanbanExport(filteredTasks, visibleColumns, sortBy);
  }, [filteredTasks, visibleColumns, s.viewMode, sortBy]);

  const handleDownloadTasksCsv = useCallback(() => {
    const projectName = selectedProject?.name ?? selectedProject?.github_repo ?? (s.selectedProjectId ?? 'all-projects');
    downloadTaskCsv(orderedExportTasks, projectName);
  }, [orderedExportTasks, selectedProject?.github_repo, selectedProject?.name, s.selectedProjectId]);

  const {
    addToast: addTaskLookupToast,
    handleViewLogs: openTaskLogs,
    tasks: taskLookupTasks,
  } = s;
  const handleViewTaskById = useCallback((taskId: string) => {
    const target = taskLookupTasks.find(t => t.id === taskId);
    if (target) {
      openTaskLogs(target);
      return;
    }
    fetchTask(taskId)
      .then(openTaskLogs)
      .catch(() => {
        addTaskLookupToast('Could not load task — it may have been deleted', 'error');
      });
  }, [addTaskLookupToast, openTaskLogs, taskLookupTasks]);

  const hasActiveFilters = s.tasks.length > 0 && (filteredTasks.length < s.tasks.length || s.failureClassFilter.length > 0);
  const visibleActiveTasks = useMemo(() => filteredTasks.filter(t => ACTIVE_PIPELINE_STATUSES.has(t.status)), [filteredTasks]);
  const visibleQueuedTasks = useMemo(() => filteredTasks.filter(t => t.status === 'QUEUED'), [filteredTasks]);
  const visibleBlockedTasks = useMemo(() => filteredTasks.filter(t => t.status === 'BLOCKED' || (t.status === 'QUEUED' && !!t.depends_on && t.depends_on.length > 0)), [filteredTasks]);
  const visibleFailedTasks = useMemo(() => filteredTasks.filter(t => t.status === 'FAILED'), [filteredTasks]);
  const visibleTerminalTasks = useMemo(() => filteredTasks.filter(t => TERMINAL_STATUSES.has(t.status)), [filteredTasks]);
  const backendPipelineAttentionTaskIds = useMemo(() => (
    strategicReadModel.data?.attention?.items
      .filter(item => item.kind === 'task' && item.ref.id)
      .map(item => item.ref.id) ?? []
  ), [strategicReadModel.data?.attention?.items]);
  const backendAttentionTasks = useMemo(() => {
    const taskById = new Map(s.tasks.map(task => [task.id, task]));
    return backendPipelineAttentionTaskIds
      .map(taskId => taskById.get(taskId))
      .filter((task): task is Task => Boolean(task));
  }, [backendPipelineAttentionTaskIds, s.tasks]);
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
  const openPipelineTask = useMemo(() => {
    if (!s.logTask) return null;
    if (s.selectedProjectId && s.logTask.project_id !== s.selectedProjectId) return null;
    return s.logTask;
  }, [s.logTask, s.selectedProjectId]);

  const [pipelineSection, setPipelineSection] = useState<PipelineSection>('flow');
  const [projectSection, setProjectSection] = useState<ProjectSection>('running-update');
  const [projectManageSection, setProjectManageSection] = useState<ProjectManageSection>('overview');
  const [runtimeSection, setRuntimeSection] = useState<RuntimeSection>('health');
  const [runtimeConfigSection, setRuntimeConfigSection] = useState<RuntimeSettingsSection>('orchestrator');
  const { setActiveTab: setPrimaryTab, setViewMode: setTaskViewMode } = s;
  useEffect(() => {
    if (!selectedProjectId || !strategicWorkbenchState?.active_surface) return;
    if (strategicWorkbenchOperatorSurfaceTouchedRef.current) return;
    if (strategicWorkbenchRestoreProjectRef.current === selectedProjectId) return;
    const tab = tabForWorkbenchSurface(strategicWorkbenchState.active_surface);
    if (!tab) return;
    strategicWorkbenchRestoreProjectRef.current = selectedProjectId;
    if (tab !== 'chat') s.setShowMessages(false);
    if (tab !== 'pipeline') s.setShowArchive(false);
    if (tab !== activeTab) setPrimaryTab(tab);
  }, [activeTab, s, selectedProjectId, setPrimaryTab, strategicWorkbenchState?.active_surface]);
  useEffect(() => {
    if (activeTab !== 'pipeline') return;
    if (s.showArchive) {
      setPipelineSection('archive');
      return;
    }
    setPipelineSection(s.viewMode === 'kanban' ? 'board' : 'flow');
  }, [activeTab, s.showArchive, s.viewMode]);
  useEffect(() => {
    if (activeTab === 'cockpit') {
      setProjectSection('running-update');
      setPrimaryTab('project');
      return;
    }
    if (activeTab === 'directions') {
      setProjectSection('directions');
      setPrimaryTab('project');
      return;
    }
    if (activeTab === 'home') {
      setRuntimeSection('health');
      setPrimaryTab('runtime');
      return;
    }
    if (activeTab === 'compute') {
      setRuntimeSection('compute');
      setPrimaryTab('runtime');
      return;
    }
    if (activeTab === 'board') {
      setPipelineSection('board');
      setTaskViewMode('kanban');
      setPrimaryTab('pipeline');
    }
  }, [activeTab, setPrimaryTab, setTaskViewMode]);
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
  const settingsRouterProps: Omit<SettingsSectionRouterProps, 'activeSection'> = {
    isAdmin: s.userRole === 'admin',
    onNavigateToSection: () => undefined,
    onToggleSettings: s.handleToggleSettings,
    allModelOptions,
    healthCheckUrlsInput,
    onHealthCheckUrlsInputChange: setHealthCheckUrlsInput,
    onUpdateHealthCheckUrls: s.updateHealthCheckUrls,
    defaultTaskType: s.defaultTaskType,
    onUpdateDefaultTaskType: s.setDefaultTaskType,
    viewMode: s.viewMode,
    onSetViewMode: s.setViewMode,
    refreshInterval: s.refreshInterval,
    onUpdateRefreshInterval: s.setRefreshInterval,
    pageSize: s.limit,
    onUpdatePageSize: s.setLimit,
    apiHealthy: s.apiHealthy,
    wsConnected: s.wsConnected,
    lastFetchTime: s.lastFetchTime,
    notifyOnComplete: s.notifyOnComplete,
    onUpdateNotifyOnComplete: s.setNotifyOnComplete,
    notifyOnFail: s.notifyOnFail,
    onUpdateNotifyOnFail: s.setNotifyOnFail,
    notifyOnApprovalNeeded: s.notifyOnApprovalNeeded,
    onUpdateNotifyOnApprovalNeeded: s.setNotifyOnApprovalNeeded,
    soundOnComplete: s.soundOnComplete,
    onUpdateSoundOnComplete: s.setSoundOnComplete,
    notifyOnHighSignalMessage: s.notifyOnHighSignalMessage,
    onUpdateNotifyOnHighSignalMessage: s.setNotifyOnHighSignalMessage,
    maxConcurrentVMs: s.maxConcurrentVMs,
    onUpdateMaxVMs: s.updateMaxVMs,
    maxRetries: s.maxRetries,
    onUpdateMaxRetries: s.updateMaxRetries,
    requireApprovalGates: s.requireApprovalGates,
    onUpdateRequireApprovalGates: s.updateRequireApprovalGates,
    autoArchiveEnabled: s.autoArchiveEnabled,
    onUpdateAutoArchiveEnabled: s.updateAutoArchiveEnabled,
    autoArchiveTerminalAfterDays: s.autoArchiveTerminalAfterDays,
    onUpdateAutoArchiveTerminalAfterDays: s.updateAutoArchiveTerminalAfterDays,
    autoArchiveMaxTerminalTasksPerProject: s.autoArchiveMaxTerminalTasksPerProject,
    onUpdateAutoArchiveMaxTerminalTasksPerProject: s.updateAutoArchiveMaxTerminalTasksPerProject,
    autoArchiveIntervalMinutes: s.autoArchiveIntervalMinutes,
    onUpdateAutoArchiveIntervalMinutes: s.updateAutoArchiveIntervalMinutes,
    maxAutonomousTasksPerHour: s.maxAutonomousTasksPerHour,
    onUpdateMaxAutonomousRate: s.handleUpdateMaxAutonomousRate,
    maxGlobalAutonomousTasksPerHour: s.maxGlobalAutonomousTasksPerHour,
    onUpdateMaxGlobalAutonomousRate: s.updateMaxGlobalAutonomousRate,
    maxPipelineSlots: s.maxPipelineSlots,
    onUpdateMaxPipelineSlots: s.updateMaxPipelineSlots,
    reservedSlotsForTypes: s.reservedSlotsForTypes,
    onUpdateReservedSlotsForTypes: s.updateReservedSlotsForTypes,
    evaluatingPipelineTimeoutMinutes: s.evaluatingPipelineTimeoutMinutes,
    onUpdateEvaluatingPipelineTimeout: s.updateEvaluatingPipelineTimeout,
    evaluatingFailHours: s.evaluatingFailHours,
    onUpdateEvaluatingFailHours: s.updateEvaluatingFailHours,
    ciBackstopTimeoutMinutes: s.ciBackstopTimeoutMinutes,
    onUpdateCiBackstopTimeout: s.updateCiBackstopTimeout,
    autonomousResearchIntervalMinutes: s.autonomousResearchIntervalMinutes,
    onUpdateAutonomousResearchInterval: s.handleUpdateAutonomousResearchInterval,
    directionCooldownMinutes: s.directionCooldownMinutes,
    onUpdateDirectionCooldownMinutes: s.handleUpdateDirectionCooldownMinutes,
    strategicResearchFrequency: s.strategicResearchFrequency,
    onUpdateStrategicResearchFrequency: s.handleUpdateStrategicResearchFrequency,
    anyAutonomousEnabled: s.projects.some(p => p.autonomous_enabled),
    onPauseAllAutonomous: s.handlePauseAllAutonomous,
    onRestartOrchestrator: s.handleRestartOrchestrator,
    githubAccount: s.githubAccount,
    onConnectGitHub: s.connectGitHub,
    maxPlanAccounts: s.maxPlanAccounts,
    onToggleAccount: s.toggleMaxPlanAccount,
    onRemoveAccount: s.removeMaxPlanAccount,
    onResetRateLimits: s.resetAccountRateLimits,
    onRefreshAccountModels: s.refreshAccountModels,
    onUpdateAccount: s.updateMaxPlanAccount,
    onUpdateAccountCaps: s.updateAccountCaps,
    showUserManagement: s.showUserManagement,
    onToggleUserManagement: () => s.setShowUserManagement(prev => !prev),
    showProjectMembers,
    onToggleProjectMembers: () => setShowProjectMembers(prev => !prev),
    selectedProjectMyRole: selectedProject?.my_role ?? null,
    newAccountName: s.newAccountName,
    setNewAccountName: s.setNewAccountName,
    newAccountProvider: s.newAccountProvider,
    setNewAccountProvider: s.setNewAccountProvider,
    newAccountAuthMode: s.newAccountAuthMode,
    setNewAccountAuthMode: s.setNewAccountAuthMode,
    newAccountToken: s.newAccountToken,
    setNewAccountToken: s.setNewAccountToken,
    newAccountProviderMetadata: s.newAccountProviderMetadata,
    setNewAccountProviderMetadata: s.setNewAccountProviderMetadata,
    newAccountClaudeConfigJson: s.newAccountClaudeConfigJson,
    setNewAccountClaudeConfigJson: s.setNewAccountClaudeConfigJson,
    newAccountDailyTaskCap: s.newAccountDailyTaskCap,
    setNewAccountDailyTaskCap: s.setNewAccountDailyTaskCap,
    newAccountMaxConcurrentTasks: s.newAccountMaxConcurrentTasks,
    setNewAccountMaxConcurrentTasks: s.setNewAccountMaxConcurrentTasks,
    newAccountAutoDisableAfterRateLimits: s.newAccountAutoDisableAfterRateLimits,
    setNewAccountAutoDisableAfterRateLimits: s.setNewAccountAutoDisableAfterRateLimits,
    onAddAccount: s.addMaxPlanAccount,
    providers: s.providers,
    providerSettingsDefaults: s.providerSettingsDefaults,
    taskTypeProviderOverrides: s.taskTypeProviderOverrides,
    providerSettingsByEffort: s.providerSettingsByEffort,
    onUpdateProviderSettingsDefaults: s.updateProviderSettingsDefaults,
    onUpdateTaskTypeProviderOverrides: s.updateTaskTypeProviderOverrides,
    onUpdateProviderSettingsByEffort: s.updateProviderSettingsByEffort,
    providerSelectionMode: s.providerSelectionMode,
    onUpdateProviderSelectionMode: s.updateProviderSelectionMode,
    taskTypeCategories: s.taskTypeCategories,
    onUpdateTaskTypeCategories: s.updateTaskTypeCategories,
    categoryRouting: s.categoryRouting,
    onUpdateCategoryRouting: s.updateCategoryRouting,
    taskTypeRouting: s.taskTypeRouting,
    onUpdateTaskTypeRouting: s.updateTaskTypeRouting,
    deployState: s.deployState,
    onPausePipeline: s.handlePausePipeline,
    onResumePipeline: s.handleResumePipeline,
    onResetHealth: s.handleResetHealth,
    onRollback: s.handleRollback,
    onMarkStable: s.handleMarkStable,
    onRepairState: s.handleRepairState,
    failedRollback: s.failedRollback,
    zoneHealth: s.zoneHealth,
    notificationConfig: s.notificationConfig,
    onUpdateNotificationConfig: s.updateNotificationConfig,
    dailyCostCeilingUsd: s.dailyCostCeilingUsd,
    onUpdateDailyCostCeiling: s.updateDailyCostCeiling,
    selectedProjectId: s.selectedProjectId,
    selectedProjectOwner: selectedProject?.github_owner ?? null,
    selectedProjectRepo: selectedProject?.github_repo ?? null,
    selectedProjectIsSelfRepo: selectedProject?.is_self_repo ?? false,
    selectedProjectRole: selectedProject?.my_role ?? null,
    buildConfig: s.buildConfig,
    onUpdateBuildConfig: s.handleUpdateBuildConfig,
    onDetectBuildConfig: s.handleDetectBuildConfig,
    autoRemediationConfig: s.autoRemediationConfig,
    autoRemediationStats: s.autoRemediationStats,
    onUpdateAutoRemediationConfig: s.handleUpdateAutoRemediationConfig,
    issueImplementationConfig: s.issueImplementationConfig,
    onUpdateIssueImplementationConfig: s.handleUpdateIssueImplementationConfig,
    autoReviewConfig: s.autoReviewConfig,
    onUpdateAutoReviewConfig: s.handleUpdateAutoReviewConfig,
    resourceEnvelopeData: s.resourceEnvelopeData,
    resourceEnvelopeLoading: s.resourceEnvelopeLoading,
    resourceEnvelopeError: s.resourceEnvelopeError,
    onUpdateResourceEnvelope: s.handleUpdateResourceEnvelope,
    integrationBranchData: s.integrationBranchData,
    onUpdateIntegrationBranch: s.handleUpdateIntegrationBranch,
    promotionTargetData: s.promotionTargetData,
    onUpdatePromotionTarget: s.handleUpdatePromotionTarget,
  };

  // activeTab drives the four operator modes: Strategic, Pipeline, Project, Runtime.
  const handleTabChange = useCallback((tab: PrimaryTab) => {
    strategicWorkbenchOperatorSurfaceTouchedRef.current = true;
    strategicWorkbenchRestoreProjectRef.current = s.selectedProjectId;
    if (tab !== 'chat') s.setShowMessages(false);
    if (tab !== 'pipeline') {
      s.setShowArchive(false);
    }
    const surface = workbenchSurfaceForTab(tab);
    if (surface) void persistStrategicWorkbench({ active_surface: surface });
    s.setActiveTab(tab);
  }, [s, persistStrategicWorkbench]);

  const focusStrategicComposer = useCallback(() => {
    handleTabChange('chat'); setStrategicComposerFocusSignal(value => value + 1); setCommandPaletteOpen(false);
  }, [handleTabChange, setCommandPaletteOpen]);
  const strategicMessageTarget = useStrategicMessageTarget(() => handleTabChange('chat'), selectedProjectId);
  const openStrategicContextTarget = useStrategicContextNavigation({
    handleTabChange, handleViewTaskById, openMessages: strategicMessageTarget.open,
    setKnowledgeBrowserDirectionId, setKnowledgeBrowserInitialDocId,
    setPipelineSection, setProjectSection, setRuntimeSection,
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
  const currentStrategicInteractionMode = normalizeStrategicInteractionMode(strategicWorkbenchState?.interaction_mode);
  const setStrategicInteractionMode = useCallback((mode: StrategicInteractionMode) => {
    void persistStrategicWorkbench({ interaction_mode: mode, active_surface: 'strategic' });
  }, [persistStrategicWorkbench]);
  const switchStrategicInteractionMode = useCallback((mode: StrategicInteractionMode) => {
    setStrategicInteractionMode(mode);
    strategicWorkbenchOperatorSurfaceTouchedRef.current = true;
    strategicWorkbenchRestoreProjectRef.current = s.selectedProjectId;
    s.setShowArchive(false);
    s.setActiveTab('chat');
  }, [s, setStrategicInteractionMode]);

  const openGlobalSettings = useCallback((section: SectionId | null = null) => {
    setSettingsInitialSection(section);
    if (!s.showSettings) s.handleToggleSettings();
    setCommandPaletteOpen(false);
  }, [s, setCommandPaletteOpen]);

  const workbenchCommands = useWorkbenchCommands({
    selectedProjectId: s.selectedProjectId,
    selectedProjectRepoLabel,
    directionsCount: directions.length,
    filteredTaskCount: filteredTasks.length,
    visibleActiveCount: visibleActiveTasks.length,
    visibleQueuedCount: visibleQueuedTasks.length,
    maxPlanAccountCount: s.maxPlanAccounts.length,
    apiHealthy: s.apiHealthy,
    problemModelRevision: strategicReadModel.data?.problem_model.revision,
    hasDesignDoc: strategicReadModel.data?.design.has_design_doc,
    designRevision: strategicReadModel.data?.design.revision,
    strategicInteractionMode: currentStrategicInteractionMode,
    strategicState: strategicReadModel.data ?? null, strategicWorkbenchState,
    savingStrategicWorkbench,
    switchStrategicInteractionMode,
    focusStrategicComposer,
    openStrategicContextTarget, openStrategicContextWorkspace, updateStrategicSessionRefs,
    handleTabChange,
    setCommandPaletteOpen,
    setPipelineSection,
    setProjectSection,
    setRuntimeSection,
    setRuntimeConfigSection,
    setViewMode: s.setViewMode,
    setShowArchive: s.setShowArchive,
    setShowActivityFeed,
    setShowResearchBrowser: s.setShowResearchBrowser,
    setShowKnowledgeBrowser: s.setShowKnowledgeBrowser,
    openNewTaskModal: s.openNewTaskModal,
    openGlobalSettings,
    refreshWorkbench: () => {
      void s.fetchTasks();
      void strategicReadModel.refresh();
    },
    openProjectManagement: () => setShowProjectManagement(true),
    openProjectMembers: () => setShowProjectMembers(true),
  });

  const { openCommandPalette } = useOperatorKeyboardShortcuts({
    activeTab,
    blockingModalOpen: Boolean(s.showNewTaskModal || s.logTask || s.showComposeDrawer || s.showFleetDetail || s.confirmDialog || showNewProjectModal || setupProjectId || showProjectManagement || showProjectMembers || strategicContextWorkspaceOpen),
    commandPaletteOpen, commands: workbenchCommands, searchInputRef,
    searchShortcutBlocked: Boolean(s.showNewTaskModal || s.logTask || s.showResearchBrowser || s.showSettings),
    selectedTaskCount: s.selectedTaskIds.size, selectionMode: s.selectionMode, setCommandPaletteOpen,
    setKnowledgeBrowserDirectionId,
    setKnowledgeBrowserInitialDocId,
    setResearchBrowserDirectionId,
    setShowActivityFeed,
    setShowAlertsPopover: s.setShowAlertsPopover,
    setShowKnowledgeBrowser: s.setShowKnowledgeBrowser,
    setShowResearchBrowser: s.setShowResearchBrowser,
    showSettings: s.showSettings, toggleSettings: s.handleToggleSettings,
  });

  const pipelineSurfaceActions = (
    <>
      <span className="hidden items-center rounded-md border border-neutral-800 bg-neutral-950/80 px-2.5 py-1.5 text-xs text-neutral-500 md:inline-flex">
        {filteredTasks.length}/{s.tasks.length}
      </span>
      <button
        type="button"
        onClick={() => { void s.fetchTasks(); }}
        className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-neutral-800 bg-neutral-950/70 px-3 text-xs font-semibold text-neutral-300 transition-colors hover:border-neutral-700 hover:bg-neutral-900 hover:text-neutral-100"
      >
        Refresh
      </button>
      <button
        type="button"
        onClick={s.openNewTaskModal}
        className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-neutral-800 bg-neutral-950/70 px-3 text-xs font-semibold text-neutral-300 transition-colors hover:border-neutral-700 hover:bg-neutral-900 hover:text-neutral-100"
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
          statusFilter={s.statusFilter}
          onStatusFilterChange={s.setStatusFilter}
          failureClassFilter={s.failureClassFilter}
          onFailureClassFilterChange={s.setFailureClassFilter}
          dateRange={s.dateRange}
          onDateRangeChange={s.setDateRange}
          blockedFilter={s.blockedFilter}
          onBlockedFilterChange={s.setBlockedFilter}
          directionFilter={s.directionFilter}
          onDirectionFilterChange={s.setDirectionFilter}
          directions={directions}
          onDownloadCsv={handleDownloadTasksCsv}
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
                ? 'border-amber-500/40 bg-amber-500/20 text-amber-400'
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
                ? 'border-amber-500/40 bg-amber-500/20 text-amber-400'
                : 'border-neutral-800/50 bg-neutral-900/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300'
            }`}
          >
            <CheckSquare className="h-3.5 w-3.5" />
            {s.selectionMode ? 'Selecting' : 'Select'}
          </button>
        )}
      </div>

      <div className="mt-2 min-w-0">
        <BoardSavedViewsBar
          key={`${s.user?.email ?? 'anon'}:${s.selectedProjectId ?? 'none'}`}
          userId={s.user?.email}
          projectId={s.selectedProjectId}
          current={boardFilterSnapshot}
          onApply={applyBoardView}
        />
        <QuickFilterPresets
          statusFilter={s.statusFilter}
          dateRange={s.dateRange}
          requiresApproval={s.requiresApproval}
          blockedFilter={s.blockedFilter}
          stuckFilter={s.stuckFilter}
          stuckCount={stuckTaskIds.size}
          stuckCount1h={stuckCount1h}
          stuckMinSeconds={s.stuckMinSeconds}
          searchQuery={s.searchQuery}
          failureClassFilter={s.failureClassFilter}
          setFailureClassFilter={s.setFailureClassFilter}
          setStatusFilter={s.setStatusFilter}
          setTaskTypeFilter={s.setTaskTypeFilter}
          setDateRange={s.setDateRange}
          setRequiresApproval={s.setRequiresApproval}
          setBlockedFilter={s.setBlockedFilter}
          setStuckFilter={s.setStuckFilter}
          setStuckMinSeconds={s.setStuckMinSeconds}
          setSearchQuery={s.setSearchQuery}
        />
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
            onClick={() => signOut(auth)}
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

  return (
    <ErrorBoundary>
    <TaskCapabilitiesProvider contract={s.taskCapabilityContract}>
    <div className="flex flex-col bg-[#08090b] text-neutral-200 font-sans p-2 md:p-4 min-h-screen md:h-screen md:overflow-hidden">
      {s.refreshing && (
        <div className="fixed top-0 left-0 right-0 z-50 h-0.5 bg-neutral-800 overflow-hidden">
          <div className="h-full w-2/5 bg-amber-500" style={{ animation: 'progress-slide 1.2s ease-in-out infinite' }} />
        </div>
      )}
      <HeaderBar
        tasks={s.tasks}
        runningTasks={runningTasks}
        queuedTasks={queuedTasks}
        globalActiveCount={s.globalActiveCount}
        globalEvaluatingCount={s.globalEvaluatingCount}
        globalQueuedCount={s.globalQueuedCount}
        maxConcurrentVMs={s.maxConcurrentVMs}
        lastRefresh={s.lastRefresh}
        onRefresh={s.fetchTasks}
        viewMode={s.viewMode}
        defaultTaskType={s.defaultTaskType}
        onUpdateDefaultTaskType={s.setDefaultTaskType}
        notifyOnComplete={s.notifyOnComplete}
        onUpdateNotifyOnComplete={s.setNotifyOnComplete}
        notifyOnFail={s.notifyOnFail}
        onUpdateNotifyOnFail={s.setNotifyOnFail}
        notifyOnApprovalNeeded={s.notifyOnApprovalNeeded}
        onUpdateNotifyOnApprovalNeeded={s.setNotifyOnApprovalNeeded}
        soundOnComplete={s.soundOnComplete}
        onUpdateSoundOnComplete={s.setSoundOnComplete}
        notifyOnHighSignalMessage={s.notifyOnHighSignalMessage}
        onUpdateNotifyOnHighSignalMessage={s.setNotifyOnHighSignalMessage}
        onSetViewMode={(mode) => {
          s.setViewMode(mode);
        }}
        showResearchBrowser={s.showResearchBrowser}
        onToggleResearchBrowser={() => s.setShowResearchBrowser(prev => !prev)}
        showKnowledgeBrowser={s.showKnowledgeBrowser}
        onToggleKnowledgeBrowser={() => s.setShowKnowledgeBrowser(prev => !prev)}
        showArchive={s.showArchive}
        onToggleArchive={() => s.setShowArchive(!s.showArchive)}
        alertCount={combinedAlerts.length}
        alertMaxSeverity={alertMaxSeverity}
        showAlertsPopover={s.showAlertsPopover}
        onToggleAlertsPopover={() => s.setShowAlertsPopover(prev => !prev)}
        onOpenActivityFeed={() => setShowActivityFeed(true)}
        openMessageCount={openMessageCount}
        newSinceLastVisitCount={newSinceLastVisit}
        needsAttentionCount={pipelineAttentionCount}
        onOpenNewTaskModal={s.openNewTaskModal}
        onNewProject={() => setShowNewProjectModal(true)}
        onManageProjects={() => setShowProjectManagement(true)}
        onSignOut={() => signOut(auth)}
        userEmail={s.user.email || ''}
        wsRef={s.wsRef}
        showSettings={s.showSettings}
        onToggleSettings={s.handleToggleSettings}
        settingsRef={s.settingsRef}
        settingsInitialSection={settingsInitialSection}
        maxRetries={s.maxRetries}
        maxAutonomousTasksPerHour={s.maxAutonomousTasksPerHour}
        maxGlobalAutonomousTasksPerHour={s.maxGlobalAutonomousTasksPerHour}
        onUpdateMaxGlobalAutonomousRate={s.updateMaxGlobalAutonomousRate}
        maxPipelineSlots={s.maxPipelineSlots}
        onUpdateMaxPipelineSlots={s.updateMaxPipelineSlots}
        reservedSlotsForTypes={s.reservedSlotsForTypes}
        onUpdateReservedSlotsForTypes={s.updateReservedSlotsForTypes}
        evaluatingPipelineTimeoutMinutes={s.evaluatingPipelineTimeoutMinutes}
        onUpdateEvaluatingPipelineTimeout={s.updateEvaluatingPipelineTimeout}
        evaluatingFailHours={s.evaluatingFailHours}
        onUpdateEvaluatingFailHours={s.updateEvaluatingFailHours}
        ciBackstopTimeoutMinutes={s.ciBackstopTimeoutMinutes}
        onUpdateCiBackstopTimeout={s.updateCiBackstopTimeout}
        requireApprovalGates={s.requireApprovalGates}
        autoArchiveEnabled={s.autoArchiveEnabled}
        autoArchiveTerminalAfterDays={s.autoArchiveTerminalAfterDays}
        autoArchiveMaxTerminalTasksPerProject={s.autoArchiveMaxTerminalTasksPerProject}
        autoArchiveIntervalMinutes={s.autoArchiveIntervalMinutes}
        onUpdateAutoArchiveEnabled={s.updateAutoArchiveEnabled}
        onUpdateAutoArchiveTerminalAfterDays={s.updateAutoArchiveTerminalAfterDays}
        onUpdateAutoArchiveMaxTerminalTasksPerProject={s.updateAutoArchiveMaxTerminalTasksPerProject}
        onUpdateAutoArchiveIntervalMinutes={s.updateAutoArchiveIntervalMinutes}
        onUpdateMaxVMs={s.updateMaxVMs}
        onUpdateMaxRetries={s.updateMaxRetries}
        onUpdateRequireApprovalGates={s.updateRequireApprovalGates}
        onUpdateMaxAutonomousRate={s.handleUpdateMaxAutonomousRate}
        autonomousResearchIntervalMinutes={s.autonomousResearchIntervalMinutes}
        onUpdateAutonomousResearchInterval={s.handleUpdateAutonomousResearchInterval}
        directionCooldownMinutes={s.directionCooldownMinutes}
        onUpdateDirectionCooldownMinutes={s.handleUpdateDirectionCooldownMinutes}
        strategicResearchFrequency={s.strategicResearchFrequency}
        onUpdateStrategicResearchFrequency={s.handleUpdateStrategicResearchFrequency}
        healthCheckUrls={s.healthCheckUrls}
        onUpdateHealthCheckUrls={s.updateHealthCheckUrls}
        githubAccount={s.githubAccount}
        onConnectGitHub={s.connectGitHub}
        maxPlanAccounts={s.maxPlanAccounts}
        onToggleAccount={s.toggleMaxPlanAccount}
        onRemoveAccount={s.removeMaxPlanAccount}
        onResetRateLimits={s.resetAccountRateLimits}
        onRefreshAccountModels={s.refreshAccountModels}
        onUpdateAccount={s.updateMaxPlanAccount}
        newAccountName={s.newAccountName}
        setNewAccountName={s.setNewAccountName}
        newAccountProvider={s.newAccountProvider}
        setNewAccountProvider={s.setNewAccountProvider}
        newAccountAuthMode={s.newAccountAuthMode}
        setNewAccountAuthMode={s.setNewAccountAuthMode}
        newAccountToken={s.newAccountToken}
        setNewAccountToken={s.setNewAccountToken}
        newAccountProviderMetadata={s.newAccountProviderMetadata}
        setNewAccountProviderMetadata={s.setNewAccountProviderMetadata}
        newAccountClaudeConfigJson={s.newAccountClaudeConfigJson}
        setNewAccountClaudeConfigJson={s.setNewAccountClaudeConfigJson}
        newAccountDailyTaskCap={s.newAccountDailyTaskCap}
        setNewAccountDailyTaskCap={s.setNewAccountDailyTaskCap}
        newAccountMaxConcurrentTasks={s.newAccountMaxConcurrentTasks}
        setNewAccountMaxConcurrentTasks={s.setNewAccountMaxConcurrentTasks}
        newAccountAutoDisableAfterRateLimits={s.newAccountAutoDisableAfterRateLimits}
        setNewAccountAutoDisableAfterRateLimits={s.setNewAccountAutoDisableAfterRateLimits}
        onAddAccount={s.addMaxPlanAccount}
        onUpdateAccountCaps={s.updateAccountCaps}
        providerTierModelMap={s.providerTierModelMap}
        taskTypeModelPreferences={s.taskTypeModelPreferences}
        providers={s.providers}
        providerSettingsDefaults={s.providerSettingsDefaults}
        taskTypeProviderOverrides={s.taskTypeProviderOverrides}
        onUpdateProviderSettingsDefaults={s.updateProviderSettingsDefaults}
        onUpdateTaskTypeProviderOverrides={s.updateTaskTypeProviderOverrides}
        providerSelectionMode={s.providerSelectionMode}
        onUpdateProviderSelectionMode={s.updateProviderSelectionMode}
        taskTypeCategories={s.taskTypeCategories}
        onUpdateTaskTypeCategories={s.updateTaskTypeCategories}
        categoryRouting={s.categoryRouting}
        onUpdateCategoryRouting={s.updateCategoryRouting}
        taskTypeRouting={s.taskTypeRouting}
        onUpdateTaskTypeRouting={s.updateTaskTypeRouting}
        providerSettingsByEffort={s.providerSettingsByEffort}
        onUpdateProviderSettingsByEffort={s.updateProviderSettingsByEffort}
        refreshInterval={s.refreshInterval}
        onUpdateRefreshInterval={s.setRefreshInterval}
        pageSize={s.limit}
        onUpdatePageSize={s.setLimit}
        apiHealthy={s.apiHealthy}
        wsConnected={s.wsConnected}
        lastFetchTime={s.lastFetchTime}
        onRestartOrchestrator={s.handleRestartOrchestrator}
        anyAutonomousEnabled={s.projects.some(p => p.autonomous_enabled)}
        onPauseAllAutonomous={s.handlePauseAllAutonomous}
        deployState={s.deployState}
        onPausePipeline={s.handlePausePipeline}
        onResumePipeline={s.handleResumePipeline}
        onResetHealth={s.handleResetHealth}
        onRollback={s.handleRollback}
        onMarkStable={s.handleMarkStable}
        onRepairState={s.handleRepairState}
        failedRollback={s.failedRollback}
        zoneHealth={s.zoneHealth}
        showUserManagement={s.showUserManagement}
        onToggleUserManagement={() => s.setShowUserManagement(prev => !prev)}
        showProjectMembers={showProjectMembers}
        onToggleProjectMembers={() => setShowProjectMembers(prev => !prev)}
        selectedProjectMyRole={selectedProject?.my_role ?? null}
        userRole={s.userRole}
        notificationConfig={s.notificationConfig}
        onUpdateNotificationConfig={s.updateNotificationConfig}
        dailyCostCeilingUsd={s.dailyCostCeilingUsd}
        onUpdateDailyCostCeiling={s.updateDailyCostCeiling}
        selectedProjectId={s.selectedProjectId}
        selectedProjectOwner={selectedProject?.github_owner ?? null}
        selectedProjectRepo={selectedProject?.github_repo ?? null}
        selectedProjectIsSelfRepo={selectedProject?.is_self_repo ?? false}
        selectedProjectRole={selectedProject?.my_role ?? null}
        buildConfig={s.buildConfig}
        onUpdateBuildConfig={s.handleUpdateBuildConfig}
        onDetectBuildConfig={s.handleDetectBuildConfig}
        autoRemediationConfig={s.autoRemediationConfig}
        autoRemediationStats={s.autoRemediationStats}
        onUpdateAutoRemediationConfig={s.handleUpdateAutoRemediationConfig}
        issueImplementationConfig={s.issueImplementationConfig}
        onUpdateIssueImplementationConfig={s.handleUpdateIssueImplementationConfig}
        autoReviewConfig={s.autoReviewConfig}
        onUpdateAutoReviewConfig={s.handleUpdateAutoReviewConfig}
        resourceEnvelopeData={s.resourceEnvelopeData}
        resourceEnvelopeLoading={s.resourceEnvelopeLoading}
        resourceEnvelopeError={s.resourceEnvelopeError}
        onUpdateResourceEnvelope={s.handleUpdateResourceEnvelope}
        integrationBranchData={s.integrationBranchData}
        onUpdateIntegrationBranch={s.handleUpdateIntegrationBranch}
        promotionTargetData={s.promotionTargetData}
        onUpdatePromotionTarget={s.handleUpdatePromotionTarget}
        projects={s.projects}
        onSelectProject={s.setSelectedProjectId}
        activeTab={activeTab}
        onTabChange={handleTabChange}
        onMessageStrategic={focusStrategicComposer}
        onOpenCommandPalette={openCommandPalette}
        health={s.orchestratorHealth}
        detailedHealth={s.detailedHealth}
        pipelineThrottle={s.pipelineThrottle}
        costSectionAvailable={activeTab === 'runtime' && runtimeSection === 'analytics'}
      />
      <CommandPaletteHost open={commandPaletteOpen} commands={workbenchCommands} query={commandPaletteQuery} onQueryChange={setCommandPaletteQuery} setOpen={setCommandPaletteOpen} />
      <StrategicContextWorkspace
        open={strategicContextWorkspaceOpen}
        state={strategicReadModel.data ?? null}
        workbenchState={strategicWorkbenchState}
        savingSessionRefs={savingStrategicWorkbench}
        onClose={closeStrategicContextWorkspace}
        onOpenTarget={openStrategicContextWorkspaceTarget}
        onUpdateSessionRefs={updateStrategicSessionRefs}
      />
      {showProjectMembers && s.selectedProjectId && selectedProject && (() => {
        const projectId = s.selectedProjectId;
        return (
        <ProjectMembers
          projectId={projectId}
          members={s.projectMembers}
          myRole={selectedProject.my_role || 'viewer'}
          onAdd={(email, role) => s.addProjectMember(projectId, email, role)}
          onUpdateRole={(memberId, role) => s.updateProjectMemberRole(projectId, memberId, role)}
          onRemove={(memberId) => s.removeProjectMember(projectId, memberId)}
        />
        );
      })()}
      <VersionMismatchBanner />
      {s.userRole === 'admin' && <NullBranchDiagnosticsPanel onConfigure={handleConfigureBranchProject} />}
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

      <WorkbenchFrame>
      {s.showUserManagement && s.userRole === 'admin' ? (
        <UserManagement
          users={s.allowedUsers}
          currentUserEmail={s.user?.email || ''}
          onAdd={s.addAllowedUser}
          onUpdateRole={s.updateAllowedUserRole}
          onRemove={s.removeAllowedUser}
          onClose={() => s.setShowUserManagement(false)}
        />
      ) : activeTab === 'chat' ? (
        <WorkbenchSurface
          title="Strategic"
          actions={(
            <StrategicSessionControl
              mode={normalizeStrategicInteractionMode(strategicWorkbenchState?.interaction_mode)}
              saving={savingStrategicWorkbench}
              localRunner={strategicReadModel.data?.runtime?.local_runner ?? null}
              runtimeWork={strategicReadModel.data?.runtime?.work ?? null}
              onModeChange={setStrategicInteractionMode}
              onOpenDevices={() => {
                setRuntimeSection('devices');
                handleTabChange('runtime');
              }}
              onOpenCompute={() => {
                setRuntimeSection('compute');
                handleTabChange('runtime');
              }}
            />
          )}
          flush
        >
          <WorkbenchSplit
            main={(
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
                targetMessage={strategicMessageTarget.target} onTargetMessageHandled={strategicMessageTarget.clear}
                strategicState={strategicReadModel.data ?? null} strategicWorkbenchState={strategicWorkbenchState}
                onStrategicStateRefresh={() => { void strategicReadModel.refresh(); }}
                onStrategicInteraction={() => { void persistStrategicWorkbench({ active_surface: 'strategic' }); }}
                onOpenStrategicContextWorkspace={openStrategicContextWorkspace}
              />
            )}
            rail={(
              <div className="space-y-3">
                <StrategicContextRail
                  state={strategicReadModel.data}
                  loading={strategicReadModel.loading}
                  error={strategicReadModel.error}
                  strategistActive={s.strategistActive}
                  cycleProgress={s.cycleProgress}
                  lastCycleCompletion={s.lastCycleCompletion}
                  onOpenProjectSection={(section) => {
                    setProjectSection(section);
                    handleTabChange('project');
                  }}
                  onOpenPipeline={() => handleTabChange('pipeline')}
                  onOpenRuntimeHealth={() => {
                    setRuntimeSection('health');
                    handleTabChange('runtime');
                  }}
                  onOpenRuntimeDevices={() => {
                    setRuntimeSection('devices');
                    handleTabChange('runtime');
                  }}
                  onOpenRuntimeCompute={() => {
                    setRuntimeSection('compute');
                    handleTabChange('runtime');
                  }}
                  onOpenTask={handleViewTaskById}
                  onOpenKnowledge={(documentId, directionId) => {
                    setKnowledgeBrowserInitialDocId(documentId ?? null);
                    setKnowledgeBrowserDirectionId(directionId ?? null);
                    s.setShowKnowledgeBrowser(true);
                  }}
                  onOpenMessages={strategicMessageTarget.open}
                  workbenchState={strategicWorkbenchState}
                  savingSessionRefs={savingStrategicWorkbench}
                  onUpdateSessionRefs={updateStrategicSessionRefs}
                />
                <TaskContextRail
                  currentTask={openPipelineTask}
                  attentionTasks={backendAttentionTasks}
                  localFallbackTasks={localAttentionTasks}
                  trackedTaskIds={s.trackedTaskIds}
                  onOpenTask={s.handleViewLogs}
                  onOpenPipeline={() => handleTabChange('pipeline')}
                  onTrackTask={s.trackTask}
                />
              </div>
            )}
          />
        </WorkbenchSurface>
      ) : activeTab !== 'pipeline' && s.showArchive ? (
        <ArchiveView
          archiveTasks={s.archiveTasks}
          archiveLoading={s.archiveLoading}
          archivePage={s.archivePage}
          archivePageSize={s.archivePageSize}
          archiveStatusFilter={s.archiveStatusFilter}
          archiveSearchQuery={s.archiveSearchQuery}
          archiveDateRange={s.archiveDateRange}
          archiveTotalTasks={s.archiveTotalTasks}
          setShowArchive={s.setShowArchive}
          setArchivePage={s.setArchivePage}
          setArchivePageSize={s.setArchivePageSize}
          setArchiveStatusFilter={s.setArchiveStatusFilter}
          setArchiveSearchQuery={s.setArchiveSearchQuery}
          setArchiveDateRange={s.setArchiveDateRange}
          handleViewLogs={s.handleViewLogs}
          onRetryTask={s.handleRetry}
          onUnarchiveTask={s.handleUnarchiveTask}
          archiveSortOrder={s.archiveSortOrder}
          setArchiveSortOrder={s.setArchiveSortOrder}
          onPurgeArchive={s.handlePurgeArchive}
        />
      ) : activeTab === 'project' ? (
        <ErrorBoundary level="panel">
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
        </ErrorBoundary>
      ) : activeTab === 'runtime' ? (
        <ErrorBoundary level="panel">
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
            onViewTask={s.handleViewLogs}
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
            onDirectionClick={(dirId) => { setResearchBrowserDirectionId(dirId); s.setShowResearchBrowser(true); }}
            onConfigureTargets={handleConfigureTargets}
            onSpawnOrphanFix={(pr) => s.openPrefilledTaskModal({ taskType: 'fix', title: pr.pr_title, prompt: `PR #${pr.pr_number} is open but has no active task - investigate and either close or push remediation. PR: ${pr.pr_url}`, prNumber: pr.pr_number, prBranch: pr.pr_branch })}
            onPanelReviewPr={(pr) => s.openPrefilledTaskModal({ taskType: 'panel_review', title: `Panel review PR #${pr.pr_number}`, prompt: '', prNumber: pr.pr_number, prBranch: pr.pr_branch, baseBranch: pr.pr_base_branch })}
            onApproveExperiment={s.handleApprove}
            onDismissExperiment={s.handleDismiss}
            onNavigateToKnowledge={(docId) => { setKnowledgeBrowserInitialDocId(docId); s.setShowKnowledgeBrowser(true); }}
            onRestartOrchestrator={s.handleRestartOrchestrator}
          />
        </ErrorBoundary>
      ) : (
        <WorkbenchSurface
          title="Pipeline"
          tabs={[
            { id: 'flow', label: 'Flow' },
            { id: 'board', label: 'Board' },
            { id: 'archive', label: 'Archive' },
          ]}
          activeTab={pipelineSection}
          onTabChange={(id) => {
            setPipelineSection(id);
            s.setShowArchive(id === 'archive');
            if (id === 'flow') s.setViewMode('pipeline');
            if (id === 'board') s.setViewMode('kanban');
          }}
          actions={pipelineSurfaceActions}
          tabLayout="stacked"
          flush
        >
          {pipelineSection === 'archive' && s.showArchive ? (
            <ArchiveView
              archiveTasks={s.archiveTasks}
              archiveLoading={s.archiveLoading}
              archivePage={s.archivePage}
              archivePageSize={s.archivePageSize}
              archiveStatusFilter={s.archiveStatusFilter}
              archiveSearchQuery={s.archiveSearchQuery}
              archiveDateRange={s.archiveDateRange}
              archiveTotalTasks={s.archiveTotalTasks}
              setShowArchive={(show) => {
                s.setShowArchive(show);
                if (!show) {
                  setPipelineSection('flow');
                  s.setViewMode('pipeline');
                }
              }}
              setArchivePage={s.setArchivePage}
              setArchivePageSize={s.setArchivePageSize}
              setArchiveStatusFilter={s.setArchiveStatusFilter}
              setArchiveSearchQuery={s.setArchiveSearchQuery}
              setArchiveDateRange={s.setArchiveDateRange}
              handleViewLogs={s.handleViewLogs}
              onRetryTask={s.handleRetry}
              onUnarchiveTask={s.handleUnarchiveTask}
              archiveSortOrder={s.archiveSortOrder}
              setArchiveSortOrder={s.setArchiveSortOrder}
              onPurgeArchive={s.handlePurgeArchive}
            />
          ) : s.viewMode === 'pipeline' ? (
            <WorkbenchSplit
              main={(
                <div className="flex h-full min-h-0 flex-col">
                  {pipelineFilterStrip}
                  <ErrorBoundary level="panel">
                    <main className="flex min-h-0 flex-1 flex-col overflow-hidden px-3 py-3 md:px-4">
                      <PipelineView
                        tasks={filteredTasks}
                        hasActiveFilters={hasActiveFilters}
                        onViewLogs={s.handleViewLogs}
                        onNavigateToParent={(parentTaskId) => {
                          navigator.clipboard.writeText(parentTaskId).then(() => {
                            s.addToast('Parent task ID copied — use search to find it', 'info');
                          }).catch(() => {
                            s.addToast(`Could not copy to clipboard — parent task ID: ${parentTaskId}`, 'error');
                          });
                        }}
                        onClearWaveDeps={s.selectedProjectId ? (() => { const pid = s.selectedProjectId; return pid ? () => s.handleClearWaveDeps(pid) : undefined; })() : undefined}
                        onArchiveGroup={(taskIds) => {
                          s.setConfirmDialog({
                            title: 'Archive this group?',
                            message: `This will archive ${taskIds.length} task${taskIds.length !== 1 ? 's' : ''} (parent + children). They can be viewed and unarchived from the Archive view.`,
                            confirmLabel: 'Archive group',
                            onConfirm: () => s.handleBulkArchiveColumn(taskIds),
                          });
                        }}
                        onUnarchiveTask={s.handleUnarchiveTask}
                      />
                    </main>
                  </ErrorBoundary>
                  {pipelinePagination}
                </div>
              )}
              rail={(
                <div className="space-y-3">
                  <WorkbenchPanel title="Pipeline" meta={hasActiveFilters ? 'filtered' : 'live'}>
                    <div className="grid grid-cols-2 gap-2">
                      <WorkbenchMetric label="active" value={visibleActiveTasks.length} tone="good" />
                      <WorkbenchMetric label="queued" value={visibleQueuedTasks.length} tone="neutral" />
                      <WorkbenchMetric label="blocked" value={visibleBlockedTasks.length} tone={visibleBlockedTasks.length > 0 ? 'warn' : 'neutral'} />
                      <WorkbenchMetric label="failed" value={visibleFailedTasks.length} tone={visibleFailedTasks.length > 0 ? 'bad' : 'neutral'} />
                    </div>
                    <div className="mt-3 flex items-center justify-between rounded-md border border-neutral-800 bg-neutral-900/40 px-3 py-2 text-xs">
                      <span className="text-neutral-500">terminal</span>
                      <span className="font-medium text-neutral-200">{visibleTerminalTasks.length}</span>
                    </div>
                  </WorkbenchPanel>

                  <TaskContextRail
                    currentTask={openPipelineTask}
                    attentionTasks={backendAttentionTasks}
                    localFallbackTasks={localAttentionTasks}
                    trackedTaskIds={s.trackedTaskIds}
                    onOpenTask={s.handleViewLogs}
                    onOpenPipeline={() => handleTabChange('pipeline')}
                    onTrackTask={s.trackTask}
                  />
                </div>
              )}
            />
          ) : (
            <div className="flex h-full min-h-0 flex-col">
              {pipelineFilterStrip}
              <ErrorBoundary level="panel">
                <div className="min-h-0 flex-1 overflow-hidden px-3 py-3 md:px-4">
                  <KanbanBoard
                    tasks={filteredTasks}
                    columns={visibleColumns}
                    loading={s.initialLoading}
                    maxRetries={s.maxRetries}
                    actionLoadingMap={s.actionLoadingMap}
                    compact={s.compactMode}
                    sortBy={sortBy}
                    selectionMode={s.selectionMode}
                    selectedTaskIds={s.selectedTaskIds}
                    onToggleTaskSelection={s.toggleTaskSelection}
                    onCtrlClick={(taskId) => { if (!s.selectionMode) s.setSelectionMode(true); s.toggleTaskSelection(taskId); }}
                    onBulkToggle={s.bulkColumnToggle}
                    onShiftSelect={s.shiftSelectRange}
                    hasActiveFilters={hasActiveFilters}
                    onArchiveColumn={(taskIds, columnLabel) => {
                      s.setConfirmDialog({
                        title: `Archive all ${columnLabel} tasks?`,
                        message: `This will archive ${taskIds.length} task${taskIds.length !== 1 ? 's' : ''} in the ${columnLabel} column. They can be viewed and unarchived from the Archive view.`,
                        confirmLabel: `Archive ${columnLabel}`,
                        onConfirm: () => s.handleBulkArchiveColumn(taskIds),
                      });
                    }}
                    onApprove={s.handleApprove}
                    onRequestChanges={s.handleRequestChanges}
                    onCancel={s.handleCancel}
                    onRetry={s.handleRetry}
                    onEditPrompt={s.handleEditPrompt}
                    onDismiss={s.handleDismiss}
                    onArchive={s.handleArchiveTask}
                    onViewLogs={s.handleViewLogs}
                    onTrack={s.trackTask}
                    trackedTaskIds={s.trackedTaskIds}
                    onDuplicate={s.openDuplicateTaskModal}
                    searchQuery={s.searchQuery}
                    onDirectionClick={(dirId) => { setResearchBrowserDirectionId(dirId); s.setShowResearchBrowser(true); }}
                  />
                </div>
              </ErrorBoundary>
              {pipelinePagination}
            </div>
          )}
        </WorkbenchSurface>
      )}
      </WorkbenchFrame>

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
            <span>{s.globalActiveCount} active</span>
            <span>{s.globalQueuedCount} queued</span>
            <span>${s.cumulativeCost.toFixed(2)}</span>
          </>
        )}
      />

      {s.showNewTaskModal && (
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
        />
      )}

      {(showNewProjectModal || setupProjectId) && (
        <NewProjectModal
          onClose={() => { setShowNewProjectModal(false); setSetupProjectId(null); }}
          onSuccess={() => {
            // Refresh project list but keep modal open so the setup guide is visible
            s.fetchProjects();
          }}
          setupProject={setupProject}
        />
      )}

      {showProjectManagement && (
        <ProjectManagementModal
          projects={s.projects}
          selectedProjectId={s.selectedProjectId}
          onNewProject={() => setShowNewProjectModal(true)}
          onRefreshProjects={s.fetchProjects}
          onSelectProject={s.setSelectedProjectId}
          onClose={() => setShowProjectManagement(false)}
        />
      )}

      {s.showResearchBrowser && (
        <ResearchBrowser
          tasks={s.tasks}
          researchFeed={researchFeed.length > 0 ? researchFeed : undefined}
          selectedProjectId={s.selectedProjectId}
          initialDirectionId={researchBrowserDirectionId ?? undefined}
          onClose={() => { s.setShowResearchBrowser(false); setResearchBrowserDirectionId(null); }}
          onNavigateToTask={(taskId) => {
            s.setShowResearchBrowser(false);
            setResearchBrowserDirectionId(null);
            const target = s.tasks.find(t => t.id === taskId);
            if (target) {
              s.handleViewLogs(target);
            } else {
              fetchTask(taskId).then(s.handleViewLogs).catch(() => {
                s.addToast('Could not load task — it may have been deleted', 'error');
              });
            }
          }}
        />
      )}

      {s.showKnowledgeBrowser && (
        <KnowledgeBrowser
          selectedProjectId={s.selectedProjectId}
          onClose={() => { s.setShowKnowledgeBrowser(false); setKnowledgeBrowserDirectionId(null); setKnowledgeBrowserInitialDocId(null); }}
          onLaunchWebResearchTask={s.openWebResearchTaskModal}
          initialDirectionId={knowledgeBrowserDirectionId ?? undefined}
          initialDocumentId={knowledgeBrowserInitialDocId ?? undefined}
          onNavigateToTask={(taskId) => {
            s.setShowKnowledgeBrowser(false);
            const target = s.tasks.find(t => t.id === taskId);
            if (target) {
              s.handleViewLogs(target);
            } else {
              fetchTask(taskId).then(s.handleViewLogs).catch(() => {
                s.addToast('Could not load task — it may have been deleted', 'error');
              });
            }
          }}
        />
      )}

      {s.logTask && (
        <ErrorBoundary level="panel">
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
          onPanelReview={(task) => {
            const payload = (task.payloads?.[0] ?? {}) as Record<string, unknown>;
            const prNumber = payload.pr_number as number | undefined;
            s.openPrefilledTaskModal({
              taskType: 'panel_review',
              title: `Panel review PR #${prNumber ?? ''}`,
              prompt: '',
              prNumber,
              prBranch: payload.branch as string | undefined,
              baseBranch: payload.base_branch as string | undefined,
            });
          }}
          onWidthChange={setDrawerWidth}
          onNavigateToTask={(taskId) => {
            const target = s.tasks.find(t => t.id === taskId);
            if (target) {
              s.handleViewLogs(target);
            } else {
              fetchTask(taskId).then(s.handleViewLogs).catch(() => {
                s.addToast('Could not load task — it may have been deleted', 'error');
              });
            }
          }}
        />
        </ErrorBoundary>
      )}

      {s.trackedTaskIds.size > 0 && (
        <TaskStreamPanel
          trackedTaskIds={s.trackedTaskIds}
          trackedLogs={s.trackedLogs}
          tasks={s.tasks}
          onUntrack={s.untrackTask}
          onUntrackAll={s.untrackAll}
          offsetRight={s.logTask ? drawerWidth : 0}
        />
      )}

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

      {showActivityFeed && (
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
      )}

      <ToastContainer toasts={s.toasts} onDismiss={s.dismissToast} />

      {s.showAlertsPopover && combinedAlerts.length > 0 && (
        <AlertsPopover
          alerts={combinedAlerts}
          onDismiss={(id) => {
            if (id.startsWith('zone-')) setDismissedZoneAlertIds(prev => new Set([...prev, id]));
            else s.dismissAlert(id);
          }}
          onDismissAll={() => {
            setDismissedZoneAlertIds(prev => new Set([...prev, ...zoneHealthAlerts.map(a => a.id)]));
            s.dismissAllAlerts();
          }}
          onViewTask={handleViewTask}
          projectId={s.selectedProjectId ?? undefined}
          onSpawnSrInvestigation={s.selectedProjectId ? handleSpawnSrInvestigation : undefined}
          activeInvestigationTaskIds={srInvestigationTaskIds}
          onClose={() => s.setShowAlertsPopover(false)}
        />
      )}

      {s.showFleetDetail && (
        <FleetDetailDrawer
          onClose={() => s.setShowFleetDetail(false)}
          onViewTask={handleViewTask}
          projects={s.projects}
          selectedProjectId={s.selectedProjectId}
        />
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
