/* eslint-disable react-hooks/refs, react-hooks/set-state-in-effect */
import { useMemo, useRef, useEffect, useState, useCallback, lazy, Suspense } from 'react';
import { ErrorBoundary } from './components/ErrorBoundary';
import { Skeleton } from './components/DashboardMetricCards';
import { Activity, AlertCircle, CheckSquare, LayoutList, PanelRightOpen } from 'lucide-react';
import { ColumnsPicker } from './components/ColumnsPicker';
import type { ColumnPref } from './components/ColumnsPicker';
import { ConfirmDialog } from './components/ConfirmDialog';
import { useAppState } from './useAppState';
import { matchesTaskTypeFilter, resolveTaskCapabilities, TaskCapabilitiesProvider } from './taskCapabilities';
import { usePoller, DEFAULT_POLL_MS } from './hooks/usePoller';
import { ResearchBrowser } from './components/ResearchBrowser';
import { KnowledgeBrowser } from './components/KnowledgeBrowser';
import { auth, signInWithGoogle, signOut } from './firebase';
import { fetchTask } from './api/tasks';
import { apiFetch, API_BASE_URL } from './api/core';
import { loadLastSeen, loadEventPrefs, loadTemporaryMutes, fetchServerNoisePrefs, fetchServerTempMutes, resolveNoisePrefs, resolveServerTempMutes, type EventPref, type TemporaryMutes } from './components/MessagesPanel.utils';
import { type SrInvestigationSignal } from './components/SrTrendBreakdown';
import { TaskDetailDrawer } from './components/TaskDetailDrawer';
import { ToastContainer } from './components/Toasts';
import { AlertsPopover } from './components/AlertsPopover';
import { FleetDetailDrawer } from './components/FleetDetailDrawer';
import { NewTaskModal } from './components/NewTaskModal';
import { NewProjectModal } from './components/NewProjectModal';
import { ProjectManagementModal } from './components/ProjectManagementModal';
import { HeaderBar, PrimaryTabNav } from './components/HeaderBar';
import { SearchFilterBar, SingleSelectDropdown } from './components/SearchFilterBar';
import type { SettingsSectionRouterProps } from './components/SettingsSectionRouter';
import { normalizePrimaryTab, resolvePrimaryTabTarget, type PrimaryTab } from './hooks/useUIState';
import { defaultComputeExperimentForm } from './hooks/useTaskModal';
import { BulkActionBar } from './components/BulkActionBar';
import type { SortBy } from './components/KanbanBoard';
import { ACTIVE_PIPELINE_STATUSES, CANCELLABLE_STATUSES, COLUMNS, SORT_KEY, TERMINAL_STATUSES } from './appWorkbenchConstants';
import { Pagination } from './components/Pagination';
import { UserManagement } from './components/UserManagement';
import { ProjectMembers } from './components/ProjectMembers';
import ActivityFeed from './components/ActivityFeed';
import { MessagesPanel } from './components/MessagesPanel';
import { StrategicContextRail } from './components/StrategicContextRail';
import { StrategicContextWorkspace } from './components/StrategicContextWorkspace';
import { StrategicSessionControl, type StrategicAccountSpendStatus, type StrategicInteractionMode } from './components/StrategicSessionControl';
import { CommandPaletteHost } from './components/CommandPaletteHost';
import { KeyboardShortcutsOverlay } from './components/KeyboardShortcutsOverlay';
import { parseArmingOutcome, workbenchArmingToastCopy } from './components/messageArming';
import { FAILURE_CLASS_CONFIG } from './components/TaskCard';
import type { Task } from './components/TaskCard';
import { VersionMismatchBanner } from './components/VersionMismatchBanner';
import { NullBranchDiagnosticsPanel } from './components/NullBranchDiagnosticsPanel';
import type { ProjectManageSection, ProjectSection, RuntimeSection, RuntimeSettingsSection } from './components/OperatorWorkspaces';
const ProjectWorkspace = lazy(() => import('./components/OperatorWorkspaces').then(m => ({ default: m.ProjectWorkspace })));
const RuntimeWorkspace = lazy(() => import('./components/OperatorWorkspaces').then(m => ({ default: m.RuntimeWorkspace })));
const PipelineWorkspace = lazy(() => import('./components/PipelineWorkspace').then(m => ({ default: m.PipelineWorkspace })));
import { WorkbenchFrame, WorkbenchSplit, WorkbenchStatusBar, WorkbenchSurface } from './components/WorkbenchLayout';
import { useStrategicReadModel } from './hooks/useStrategicReadModel';
import { useOperatorKeyboardShortcuts } from './hooks/useOperatorKeyboardShortcuts';
import { useStrategicMessageTarget } from './hooks/useStrategicMessageTarget';
import { useCommandPaletteState } from './hooks/useCommandPaletteState';
import { useStrategicContextNavigation } from './hooks/useStrategicContextNavigation';
import { useWorkbenchCommands, type PipelineSection } from './hooks/useWorkbenchCommands';
import { useVersionLifecycle, hasVersionLifecycleData } from './hooks/useVersionLifecycle';
import { checkInStrategicSession, startStrategicSession, stopStrategicSession, updateStrategicWorkbenchState, type StrategicResidentRunnerOption, type StrategicResidentSessionSnapshot, type StrategicStateAttentionItem, type StrategicWorkbenchState } from './strategicStateApi';
import { parseTimestamp } from './utils/time';
import { matchesStatusFilter } from './utils/statusFilter';
import { buildOptimisticStrategicWorkbench, normalizeStrategicInteractionMode, tabForWorkbenchSurface, workbenchSurfaceForTab } from './utils/strategicWorkbenchSession';
import { pipelineTaskIdForAttentionItem } from './utils/strategicAttention';
import { computeStrategicSessionActivity } from './utils/strategicSessionLive';
import { buildStrategicContextDescriptor } from './utils/strategicContextDescriptor';
import { STATUS_OPTIONS } from './constants';

type StrategicSessionRefs = StrategicWorkbenchState['selected_refs'];
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

export default function App() {
  const refreshStrategicReadModelRef = useRef<() => void>(() => {});
  const refreshMessagesRef = useRef<() => void>(() => {});
  const refreshFleetRunnersRef = useRef<() => void>(() => {});
  const [messageRealtimeRefreshSignal, setMessageRealtimeRefreshSignal] = useState(0);
  refreshMessagesRef.current = () => setMessageRealtimeRefreshSignal(value => value + 1);
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
  const selectedProjectIdRef = useRef(selectedProjectId);
  selectedProjectIdRef.current = selectedProjectId;
  const rawActiveTab = s.activeTab;
  const activeTab: PrimaryTab = normalizePrimaryTab(rawActiveTab);
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
        setOpenMessageCount(
          (data.blocker ?? 0) + (data.question ?? 0) + (data.bug_report ?? 0) + (data.feedback ?? 0) + (data.high_signal_note ?? 0),
        );
        setNewSinceLastVisit(data.unread ?? 0);
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
  const alertMaxSeverity: 'ERROR' | 'WARNING' | 'INFO' | null = combinedAlerts.some(a => a.severity === 'ERROR') ? 'ERROR'
    : combinedAlerts.some(a => a.severity === 'WARNING') ? 'WARNING'
    : combinedAlerts.length > 0 ? 'INFO' : null;

  const strategicReadModel = useStrategicReadModel(s.selectedProjectId, { pollerEnabled: activeTab === 'chat' });
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
  const [checkingInStrategicSession, setCheckingInStrategicSession] = useState(false);
  const [strategicSessionBusy, setStrategicSessionBusy] = useState(false);
  const strategicWorkbenchStateRef = useRef<StrategicWorkbenchState | null>(null);
  const strategicWorkbenchSaveSeqRef = useRef(0);
  const strategicWorkbenchSaveQueueRef = useRef<Promise<void>>(Promise.resolve());
  const strategicCheckInIdempotencyKeyRef = useRef<string | null>(null);
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
    strategicCheckInIdempotencyKeyRef.current = null;
    setCheckingInStrategicSession(false);
    setStrategicSessionBusy(false);
  }, [selectedProjectId]);
  const strategicReadModelWorkbench = strategicReadModel.data?.project_id === selectedProjectId
    ? strategicReadModel.data?.workbench ?? null
    : null;
  const selectedStrategicReadModel = strategicReadModel.data?.project_id === selectedProjectId
    ? strategicReadModel.data
    : null;
  // Resident Strategic session state is surfaced on the strategic read model as
  // `resident_session` (mirrors `resident_session_snapshot`). The cast keeps
  // this forward-compatible whether or not the read-model type already declares
  // the field. `sessionActive` intentionally excludes a session whose stop was
  // requested so the control flips back to Start immediately on stop.
  const residentStrategicSession =
    (selectedStrategicReadModel as { resident_session?: StrategicResidentSessionSnapshot | null } | null)?.resident_session
    ?? null;
  const strategicSessionActive = residentStrategicSession?.status === 'active' && !residentStrategicSession?.stop_requested;
  const strategicSessionStatus = residentStrategicSession?.status ?? null;
  // The Start affordance is gated on the resident-session feature flag, surfaced
  // as `resident_session_available` on the read model. When the flag is off we
  // withhold `onStartSession` so StrategicSessionControl (which self-gates on
  // callback presence) hides Start, rather than letting a click 403 at the start
  // endpoint. Stop stays wired unconditionally so an operator can always reclaim.
  const residentStrategicSessionAvailable =
    (selectedStrategicReadModel as { resident_session_available?: boolean } | null)?.resident_session_available === true;
  // Legible Workday session state + reason (from `resident_session_availability`):
  // drives the status chip (off / ready / starting / active / blocked / error) so
  // entering Workday is enough to go live. Falls back to a flag-derived state for
  // older payloads.
  const residentStrategicSessionAvailability =
    (selectedStrategicReadModel as {
      resident_session_availability?: { available?: boolean; state?: string | null; reason?: string | null } | null;
    } | null)?.resident_session_availability ?? null;
  const strategicSessionState = residentStrategicSessionAvailability?.state
    ?? (strategicSessionActive ? 'active' : residentStrategicSessionAvailable ? 'ready' : 'off');
  const strategicSessionReason = residentStrategicSessionAvailability?.reason ?? null;
  const strategicWorkbenchState = optimisticStrategicWorkbench ?? strategicReadModelWorkbench;
  // Multi-device picker options for the resident session (auto-pick + override
  // dropdown). The selected device is the operator's stored preference, falling
  // back to the device the live session is pinned to so an active session shows
  // its actual runner.
  const residentStrategicSessionRunners =
    (selectedStrategicReadModel as { resident_session_runners?: StrategicResidentRunnerOption[] | null } | null)
      ?.resident_session_runners ?? null;
  const strategicSessionSelectedRunnerId =
    strategicWorkbenchState?.preferred_runner_id
    ?? residentStrategicSession?.runner_id
    ?? null;
  const chatComposerContextDescriptor = useMemo(
    () => buildStrategicContextDescriptor(selectedStrategicReadModel, strategicWorkbenchState),
    [selectedStrategicReadModel, strategicWorkbenchState],
  );
  useEffect(() => {
    strategicWorkbenchStateRef.current = strategicWorkbenchState;
  }, [strategicWorkbenchState]);
  // The optimistic workbench shadow is only meant to cover the in-flight save
  // window. Once no save is pending, let the polled server read model reclaim
  // the display as soon as it is at least as fresh as the optimistic value.
  // Without this the shadow would outlive its mutation forever (cleared only on
  // project switch), so workbench/session changes arriving via the 30s poll —
  // e.g. from another device or session — never surfaced.
  useEffect(() => {
    if (savingStrategicWorkbench) return;
    if (!optimisticStrategicWorkbench || !strategicReadModelWorkbench) return;
    const optimisticTs = parseTimestamp(optimisticStrategicWorkbench.last_interaction_at).getTime();
    const serverTs = parseTimestamp(strategicReadModelWorkbench.last_interaction_at).getTime();
    if (serverTs >= optimisticTs) {
      setOptimisticStrategicWorkbench(null);
    }
  }, [savingStrategicWorkbench, optimisticStrategicWorkbench, strategicReadModelWorkbench]);
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
          const result = await updateStrategicWorkbenchState(projectId, patch);
          const armingOutcome = selectedProjectIdRef.current === projectId ? parseArmingOutcome(result.arming) : null;
          const armingToast = workbenchArmingToastCopy(armingOutcome);
          if (armingToast) s.addToast(armingToast, armingOutcome === 'not_armed' ? 'info' : 'success');
          if (selectedProjectIdRef.current !== projectId || strategicWorkbenchSaveSeqRef.current !== saveSeq) return;
          const saved = result.workbench;
          strategicWorkbenchStateRef.current = saved;
          setOptimisticStrategicWorkbench(saved);
          void strategicReadModel.refresh();
        } catch {
          if (selectedProjectIdRef.current === projectId && strategicWorkbenchSaveSeqRef.current === saveSeq) {
            strategicWorkbenchStateRef.current = base;
            setOptimisticStrategicWorkbench(base);
            s.addToast('Could not save Strategic session', 'error');
          }
        } finally {
          if (selectedProjectIdRef.current === projectId && strategicWorkbenchSaveSeqRef.current === saveSeq) setSavingStrategicWorkbench(false);
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
  // Multi-device override: pick which enrolled runner the resident session pins
  // to. Persisted as the workbench `preferred_runner_id` — the source the Start
  // handler already reads — so the next start (or restart after a device goes
  // offline) uses the chosen device. `null` selects auto-pick.
  const handleSelectStrategicRunner = useCallback((runnerId: string | null) => {
    persistStrategicWorkbench({ preferred_runner_id: runnerId });
  }, [persistStrategicWorkbench]);
  const handleStrategicCheckIn = useCallback(async () => {
    const projectId = s.selectedProjectId;
    if (!projectId || checkingInStrategicSession) return;
    setCheckingInStrategicSession(true);
    try {
      const workbenchContext = strategicWorkbenchStateRef.current ?? strategicReadModelWorkbench;
      if (!strategicCheckInIdempotencyKeyRef.current) {
        strategicCheckInIdempotencyKeyRef.current = `strategic-check-in:${projectId}:${Date.now()}`;
      }
      const checkInResponse = await checkInStrategicSession(projectId, {
        kind: 'manual',
        workbench_context: workbenchContext ? { ...workbenchContext } : undefined,
        idempotency_key: strategicCheckInIdempotencyKeyRef.current,
      });
      if (selectedProjectIdRef.current !== projectId) return;
      strategicCheckInIdempotencyKeyRef.current = null;
      s.addToast('Strategic check-in recorded', 'success');
      if (strategicReadModel.data) {
        strategicReadModel.applySessionResponse(checkInResponse);
      } else {
        void strategicReadModel.refresh();
      }
    } catch {
      if (selectedProjectIdRef.current === projectId) {
        s.addToast('Could not record Strategic check-in', 'error');
      }
    } finally {
      if (selectedProjectIdRef.current === projectId) setCheckingInStrategicSession(false);
    }
  }, [checkingInStrategicSession, s, strategicReadModel, strategicReadModelWorkbench]);
  const handleStartStrategicSession = useCallback(async () => {
    const projectId = s.selectedProjectId;
    if (!projectId || strategicSessionBusy) return;
    setStrategicSessionBusy(true);
    try {
      const preferredRunnerId = (strategicWorkbenchStateRef.current ?? strategicReadModelWorkbench)?.preferred_runner_id ?? null;
      await startStrategicSession(projectId, preferredRunnerId ? { runner_id: preferredRunnerId } : {});
      if (selectedProjectIdRef.current !== projectId) return;
      s.addToast('Strategic session starting', 'success');
      void strategicReadModel.refresh();
    } catch (err) {
      if (selectedProjectIdRef.current === projectId) {
        s.addToast(err instanceof Error ? err.message : 'Could not start Strategic session', 'error');
      }
    } finally {
      if (selectedProjectIdRef.current === projectId) setStrategicSessionBusy(false);
    }
  }, [strategicSessionBusy, s, strategicReadModel, strategicReadModelWorkbench]);
  const handleStopStrategicSession = useCallback(async () => {
    const projectId = s.selectedProjectId;
    if (!projectId || strategicSessionBusy) return;
    setStrategicSessionBusy(true);
    try {
      await stopStrategicSession(projectId);
      if (selectedProjectIdRef.current !== projectId) return;
      s.addToast('Strategic session stopping', 'success');
      void strategicReadModel.refresh();
    } catch (err) {
      if (selectedProjectIdRef.current === projectId) {
        s.addToast(err instanceof Error ? err.message : 'Could not stop Strategic session', 'error');
      }
    } finally {
      if (selectedProjectIdRef.current === projectId) setStrategicSessionBusy(false);
    }
  }, [strategicSessionBusy, s, strategicReadModel]);
  const taskContextSessionProps = useMemo(() => ({
    sessionRefs: strategicWorkbenchState?.selected_refs,
    savingSessionRefs: savingStrategicWorkbench,
    onUpdateSessionRefs: updateStrategicSessionRefs,
  }), [strategicWorkbenchState?.selected_refs, savingStrategicWorkbench, updateStrategicSessionRefs]);
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
      (counts.parked_tasks ?? 0)
    );
  }, [s.needsAttentionCount, strategicReadModel.data?.attention?.counts]);
  const { open: commandPaletteOpen, query: commandPaletteQuery, setOpen: setCommandPaletteOpen, setQuery: setCommandPaletteQuery } = useCommandPaletteState();
  const [shortcutHelpOpen, setShortcutHelpOpen] = useState(false);
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
  const { versionLifecycle, versionLifecycleError, versionLifecycleLastSuccessAt, versionLifecycleRefreshing, refreshVersionLifecycle } = useVersionLifecycle(s.selectedProjectId);
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
    if (!selectedProjectId || !strategicWorkbenchState?.active_surface) return;
    if (strategicWorkbenchOperatorSurfaceTouchedRef.current) return;
    if (strategicWorkbenchRestoreProjectRef.current === selectedProjectId) return;
    const tab = tabForWorkbenchSurface(strategicWorkbenchState.active_surface);
    if (!tab) return;
    strategicWorkbenchRestoreProjectRef.current = selectedProjectId;
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
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const settingsRouterProps: Omit<SettingsSectionRouterProps, 'activeSection'> = useMemo(() => ({
    isAdmin: s.userRole === 'admin',
    onNavigateToSection: (section: string) => {
      if (section === 'notifications') {
        setRuntimeSection('settings');
        setRuntimeConfigSection('notifications');
      }
    },
    hostKind: 'workspace',
    closeOnExternalPanelOpen: false,
    onToggleSettings: ignoreEmbeddedSettingsClose,
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
    directionChurnWindowCycles: s.directionChurnWindowCycles,
    onUpdateDirectionChurnWindowCycles: s.updateDirectionChurnWindowCycles,
    directionChurnNonMergeThreshold: s.directionChurnNonMergeThreshold,
    onUpdateDirectionChurnNonMergeThreshold: s.updateDirectionChurnNonMergeThreshold,
    directionChurnMinMergeRate: s.directionChurnMinMergeRate,
    onUpdateDirectionChurnMinMergeRate: s.updateDirectionChurnMinMergeRate,
    directionLifetimeNonMergeThreshold: s.directionLifetimeNonMergeThreshold,
    onUpdateDirectionLifetimeNonMergeThreshold: s.updateDirectionLifetimeNonMergeThreshold,
    directionLifetimeMinMergeRate: s.directionLifetimeMinMergeRate,
    onUpdateDirectionLifetimeMinMergeRate: s.updateDirectionLifetimeMinMergeRate,
    directionReviewRecommendationThreshold: s.directionReviewRecommendationThreshold,
    onUpdateDirectionReviewRecommendationThreshold: s.updateDirectionReviewRecommendationThreshold,
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
    selectedProjectOnboardingProof: selectedProject?.onboarding_proof ?? null,
  }), [
    s.userRole,
    allModelOptions,
    healthCheckUrlsInput,
    s.updateHealthCheckUrls,
    s.defaultTaskType,
    s.setDefaultTaskType,
    s.viewMode,
    s.setViewMode,
    s.refreshInterval,
    s.setRefreshInterval,
    s.limit,
    s.setLimit,
    s.apiHealthy,
    s.wsConnected,
    s.lastFetchTime,
    s.notifyOnComplete,
    s.setNotifyOnComplete,
    s.notifyOnFail,
    s.setNotifyOnFail,
    s.notifyOnApprovalNeeded,
    s.setNotifyOnApprovalNeeded,
    s.soundOnComplete,
    s.setSoundOnComplete,
    s.notifyOnHighSignalMessage,
    s.setNotifyOnHighSignalMessage,
    s.maxConcurrentVMs,
    s.updateMaxVMs,
    s.maxRetries,
    s.updateMaxRetries,
    s.requireApprovalGates,
    s.updateRequireApprovalGates,
    s.autoArchiveEnabled,
    s.updateAutoArchiveEnabled,
    s.autoArchiveTerminalAfterDays,
    s.updateAutoArchiveTerminalAfterDays,
    s.autoArchiveMaxTerminalTasksPerProject,
    s.updateAutoArchiveMaxTerminalTasksPerProject,
    s.autoArchiveIntervalMinutes,
    s.updateAutoArchiveIntervalMinutes,
    s.maxAutonomousTasksPerHour,
    s.handleUpdateMaxAutonomousRate,
    s.maxGlobalAutonomousTasksPerHour,
    s.updateMaxGlobalAutonomousRate,
    s.maxPipelineSlots,
    s.updateMaxPipelineSlots,
    s.reservedSlotsForTypes,
    s.updateReservedSlotsForTypes,
    s.evaluatingPipelineTimeoutMinutes,
    s.updateEvaluatingPipelineTimeout,
    s.evaluatingFailHours,
    s.updateEvaluatingFailHours,
    s.ciBackstopTimeoutMinutes,
    s.updateCiBackstopTimeout,
    s.autonomousResearchIntervalMinutes,
    s.handleUpdateAutonomousResearchInterval,
    s.directionCooldownMinutes,
    s.handleUpdateDirectionCooldownMinutes,
    s.strategicResearchFrequency,
    s.handleUpdateStrategicResearchFrequency,
    s.projects,
    s.handlePauseAllAutonomous,
    s.handleRestartOrchestrator,
    s.directionChurnWindowCycles,
    s.updateDirectionChurnWindowCycles,
    s.directionChurnNonMergeThreshold,
    s.updateDirectionChurnNonMergeThreshold,
    s.directionChurnMinMergeRate,
    s.updateDirectionChurnMinMergeRate,
    s.directionLifetimeNonMergeThreshold,
    s.updateDirectionLifetimeNonMergeThreshold,
    s.directionLifetimeMinMergeRate,
    s.updateDirectionLifetimeMinMergeRate,
    s.directionReviewRecommendationThreshold,
    s.updateDirectionReviewRecommendationThreshold,
    s.githubAccount,
    s.connectGitHub,
    s.maxPlanAccounts,
    s.toggleMaxPlanAccount,
    s.removeMaxPlanAccount,
    s.resetAccountRateLimits,
    s.refreshAccountModels,
    s.updateMaxPlanAccount,
    s.updateAccountCaps,
    s.showUserManagement,
    s.setShowUserManagement,
    showProjectMembers,
    selectedProject?.my_role,
    s.newAccountName,
    s.setNewAccountName,
    s.newAccountProvider,
    s.setNewAccountProvider,
    s.newAccountAuthMode,
    s.setNewAccountAuthMode,
    s.newAccountToken,
    s.setNewAccountToken,
    s.newAccountProviderMetadata,
    s.setNewAccountProviderMetadata,
    s.newAccountClaudeConfigJson,
    s.setNewAccountClaudeConfigJson,
    s.newAccountDailyTaskCap,
    s.setNewAccountDailyTaskCap,
    s.newAccountMaxConcurrentTasks,
    s.setNewAccountMaxConcurrentTasks,
    s.newAccountAutoDisableAfterRateLimits,
    s.setNewAccountAutoDisableAfterRateLimits,
    s.addMaxPlanAccount,
    s.providers,
    s.providerSettingsDefaults,
    s.updateProviderSettingsDefaults,
    s.taskTypeProviderOverrides,
    s.updateTaskTypeProviderOverrides,
    s.providerSettingsByEffort,
    s.updateProviderSettingsByEffort,
    s.providerSelectionMode,
    s.updateProviderSelectionMode,
    s.taskTypeCategories,
    s.updateTaskTypeCategories,
    s.categoryRouting,
    s.updateCategoryRouting,
    s.taskTypeRouting,
    s.updateTaskTypeRouting,
    s.deployState,
    s.handlePausePipeline,
    s.handleResumePipeline,
    s.handleResetHealth,
    s.handleRollback,
    s.handleMarkStable,
    s.handleRepairState,
    s.failedRollback,
    s.zoneHealth,
    s.notificationConfig,
    s.updateNotificationConfig,
    s.dailyCostCeilingUsd,
    s.updateDailyCostCeiling,
    s.selectedProjectId,
    selectedProject?.github_owner,
    selectedProject?.github_repo,
    selectedProject?.is_self_repo,
    selectedProject?.my_role,
    s.buildConfig,
    s.handleUpdateBuildConfig,
    s.handleDetectBuildConfig,
    s.autoRemediationConfig,
    s.autoRemediationStats,
    s.handleUpdateAutoRemediationConfig,
    s.issueImplementationConfig,
    s.handleUpdateIssueImplementationConfig,
    s.autoReviewConfig,
    s.handleUpdateAutoReviewConfig,
    s.resourceEnvelopeData,
    s.resourceEnvelopeLoading,
    s.resourceEnvelopeError,
    s.handleUpdateResourceEnvelope,
    s.integrationBranchData,
    s.handleUpdateIntegrationBranch,
    s.promotionTargetData,
    s.handleUpdatePromotionTarget,
    selectedProject?.onboarding_proof,
    ignoreEmbeddedSettingsClose,
  ]);

  // activeTab drives the four operator modes: Strategic, Pipeline, Project, Runtime.
  const handleTabChange = useCallback((tab: PrimaryTab) => {
    strategicWorkbenchOperatorSurfaceTouchedRef.current = true;
    strategicWorkbenchRestoreProjectRef.current = s.selectedProjectId;
    if (tab !== 'pipeline') {
      s.setShowArchive(false);
    }
    const surface = workbenchSurfaceForTab(tab);
    if (surface) void persistStrategicWorkbench({ active_surface: surface });
    s.setActiveTab(tab);
  }, [s, persistStrategicWorkbench]);

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
  const currentStrategicInteractionMode = normalizeStrategicInteractionMode(strategicWorkbenchState?.interaction_mode);
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
  const railLiveActivity = useMemo(
    () => computeStrategicSessionActivity(s.strategicSessionTranscript, []),
    [s.strategicSessionTranscript],
  );
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

  const workbenchCommands = useWorkbenchCommands({
    selectedProjectId: s.selectedProjectId,
    selectedProjectRepoLabel,
    hasVersionLifecycle,
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
    strategicInteractionMode: currentStrategicInteractionMode,
    strategicState: strategicReadModel.data ?? null, strategicWorkbenchState,
    savingStrategicWorkbench,
    checkingInStrategicSession,
    switchStrategicInteractionMode,
    markStrategicChecked: handleStrategicCheckIn,
    focusStrategicComposer,
    openStrategicContextTarget: openStrategicContextWorkspaceTarget,
    openStrategicContextWorkspace,
    openStrategicMessageTarget: openStrategicMessageTargetFromCommandPalette,
    updateStrategicSessionRefs,
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
    s.showAlertsPopover ||
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
    setShowAlertsPopover: s.setShowAlertsPopover,
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
        alertCount={combinedAlerts.length}
        alertMaxSeverity={alertMaxSeverity}
        showAlertsPopover={s.showAlertsPopover}
        onToggleAlertsPopover={() => s.setShowAlertsPopover(prev => !prev)}
        onOpenActivityFeed={() => setShowActivityFeed(true)}
        openMessageCount={openMessageCount}
        newSinceLastVisitCount={newSinceLastVisit}
        needsAttentionCount={pipelineAttentionCount}
        onOpenNewTaskModal={s.openNewTaskModal}
        onOpenSettings={() => openRuntimeSettings('general')}
        onNewProject={() => setShowNewProjectModal(true)}
        onManageProjects={() => setShowProjectManagement(true)}
        onSignOut={() => signOut(auth)}
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
      />
      <CommandPaletteHost open={commandPaletteOpen} commands={workbenchCommands} query={commandPaletteQuery} onQueryChange={setCommandPaletteQuery} setOpen={setCommandPaletteOpen} />
      <KeyboardShortcutsOverlay open={shortcutHelpOpen} onClose={() => setShortcutHelpOpen(false)} />
      <StrategicContextWorkspace
        open={strategicContextWorkspaceOpen}
        state={strategicReadModel.data ?? null}
        workbenchState={strategicWorkbenchState}
        savingSession={savingStrategicWorkbench}
        savingSessionRefs={savingStrategicWorkbench}
        accountSpend={strategicAccountSpend}
        sessionActive={strategicSessionActive}
        sessionStatus={strategicSessionStatus}
        sessionState={strategicSessionState}
        sessionReason={strategicSessionReason}
        sessionBusy={strategicSessionBusy}
        sessionRunners={residentStrategicSessionRunners}
        sessionSelectedRunnerId={strategicSessionSelectedRunnerId}
        onSelectRunner={handleSelectStrategicRunner}
        onStartSession={residentStrategicSessionAvailable ? handleStartStrategicSession : undefined}
        onStopSession={handleStopStrategicSession}
        onClose={closeStrategicContextWorkspace}
        onOpenTarget={openStrategicContextWorkspaceTarget}
        onOpenMessageTarget={strategicMessageTarget.open}
        onModeChange={setStrategicInteractionMode}
        onOpenAccounts={openRuntimeAccounts}
        onOpenSpend={openRuntimeAnalytics}
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
          showTitle={false}
          actions={(
            <>
              <StrategicSessionControl
                mode={normalizeStrategicInteractionMode(strategicWorkbenchState?.interaction_mode)}
                saving={savingStrategicWorkbench}
                localRunner={strategicReadModel.data?.runtime?.local_runner ?? null}
                runtimeWork={strategicReadModel.data?.runtime?.work ?? null}
                accountSpend={strategicAccountSpend}
                sessionActive={strategicSessionActive}
                sessionStatus={strategicSessionStatus}
                sessionState={strategicSessionState}
                sessionReason={strategicSessionReason}
                sessionBusy={strategicSessionBusy}
                sessionRunners={residentStrategicSessionRunners}
                sessionSelectedRunnerId={strategicSessionSelectedRunnerId}
                onSelectRunner={handleSelectStrategicRunner}
                onStartSession={residentStrategicSessionAvailable ? handleStartStrategicSession : undefined}
                onStopSession={handleStopStrategicSession}
                onModeChange={setStrategicInteractionMode}
                onOpenDevices={() => {
                  setRuntimeSection('devices');
                  handleTabChange('runtime');
                }}
                onOpenCompute={() => {
                  setRuntimeSection('compute');
                  handleTabChange('runtime');
                }}
                onOpenAccounts={openRuntimeAccounts}
                onOpenSpend={openRuntimeAnalytics}
                showMonitoringControls={false}
                density="compact"
                hideDetailsOnMobile
                className="flex"
              />
              {!chatComposerContextDescriptor && (
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
              )}
            </>
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
                strategicSessionTranscript={s.strategicSessionTranscript}
                focusComposerSignal={strategicComposerFocusSignal}
                realtimeRefreshSignal={messageRealtimeRefreshSignal}
                targetMessage={strategicMessageTarget.target} onTargetMessageHandled={strategicMessageTarget.clear}
                strategicState={strategicReadModel.data ?? null} strategicWorkbenchState={strategicWorkbenchState}
                onStrategicStateRefresh={() => { void strategicReadModel.refresh(); }}
                onStrategicInteraction={() => { void persistStrategicWorkbench({ active_surface: 'strategic' }); }}
                onOpenCommandPalette={openCommandPalette}
                onOpenStrategicContextWorkspace={openStrategicContextWorkspace}
              />
            )}
            rail={(
              <StrategicContextRail
                state={strategicReadModel.data ?? null}
                loading={strategicReadModel.loading}
                error={strategicReadModel.error}
                strategistActive={s.strategistActive}
                cycleProgress={s.cycleProgress}
                lastCycleCompletion={s.lastCycleCompletion}
                liveActivity={railLiveActivity}
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
                workbenchState={strategicWorkbenchState}
                accountSpend={strategicAccountSpend}
                savingSessionRefs={savingStrategicWorkbench}
                onUpdateSessionRefs={updateStrategicSessionRefs}
              />
            )}
            railClassName="bg-workbench-rail"
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
            onPanelReviewPr={(pr) => s.openPrefilledTaskModal({ taskType: 'panel_review', title: `Panel review PR #${pr.pr_number}`, prompt: '', prNumber: pr.pr_number, prBranch: pr.pr_branch, baseBranch: pr.pr_base_branch })}
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
            taskContextSessionProps,
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
            handleViewTaskById(taskId);
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
            handleViewTaskById(taskId);
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
          onNavigateToTask={(taskId) => {
            handleViewTaskById(taskId);
          }}
        />
        </ErrorBoundary>
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
          fleetRunnersRefreshSignal={s.fleetRunnersRefreshSignal}
          live={s.wsConnected}
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
