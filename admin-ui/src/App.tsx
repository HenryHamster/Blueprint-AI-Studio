import { useMemo, useRef, useEffect, useState, useCallback } from 'react';
import { ErrorBoundary } from './components/ErrorBoundary';
import { Activity, AlertCircle, LayoutList, CheckSquare, Microscope, BookOpen } from 'lucide-react';
import { ColumnsPicker } from './components/ColumnsPicker';
import type { ColumnPref } from './components/ColumnsPicker';
import { ConfirmDialog } from './components/ConfirmDialog';
import { useAppState } from './useAppState';
import { matchesTaskTypeFilter, resolveTaskCapabilities, TaskCapabilitiesProvider } from './taskCapabilities';
import { usePoller, DEFAULT_POLL_MS } from './hooks/usePoller';
import { DirectionsPanel } from './components/DirectionsPanel';
import TaskStreamPanel from './components/TaskStreamPanel';
import { ResearchBrowser } from './components/ResearchBrowser';
import { KnowledgeBrowser } from './components/KnowledgeBrowser';
import { PipelineView } from './components/PipelineView';
import { auth, signInWithGoogle, signOut } from './firebase';
import { fetchTask, apiFetch, API_BASE_URL } from './api';
import { countHighSignalMessages, countNewSinceLastVisit, loadLastSeen, fetchBackendLastSeen, resolveLastSeen, loadEventPrefs, loadTemporaryMutes, fetchServerNoisePrefs, fetchServerTempMutes, resolveNoisePrefs, resolveServerTempMutes, type EventPref, type TemporaryMutes } from './components/MessagesPanel.utils';
import { type SrInvestigationSignal } from './components/SrTrendBreakdown';
import { TaskDetailDrawer } from './components/TaskDetailDrawer';
import { ToastContainer } from './components/Toasts';
import { AlertsPopover } from './components/AlertsPopover';
import { ComposeDrawer } from './components/ComposeDrawer';
import { FleetDetailDrawer } from './components/FleetDetailDrawer';
import { NewTaskModal } from './components/NewTaskModal';
import { NewProjectModal } from './components/NewProjectModal';
import { HeaderBar } from './components/HeaderBar';
import type { SectionId } from './components/SettingsDropdown';
import MetricsDashboard from './components/MetricsDashboard';
import { SearchFilterBar, SingleSelectDropdown } from './components/SearchFilterBar';
import { DashboardHome } from './components/DashboardHome';
import { ConsoleHome } from './components/ConsoleHome';
import { ProjectSwitcherDropdown } from './components/ProjectSwitcherDropdown';
import type { PrimaryTab } from './hooks/useUIState';
import { BulkActionBar } from './components/BulkActionBar';
import { KanbanBoard } from './components/KanbanBoard';
import type { KanbanColumn, SortBy } from './components/KanbanBoard';
import { downloadTaskCsv, orderTasksForKanbanExport } from './utils/taskCsv';
import { Pagination } from './components/Pagination';
import ArchiveView from './components/ArchiveView';
import { UserManagement } from './components/UserManagement';
import { ProjectMembers } from './components/ProjectMembers';
import ActivityFeed from './components/ActivityFeed';
import { MessagesPanel } from './components/MessagesPanel';
import { QuickFilterPresets } from './components/QuickFilterPresets';
import { BoardSavedViewsBar, type BoardFilterSnapshot } from './components/BoardSavedViewsBar';
import { FAILURE_CLASS_CONFIG } from './components/TaskCard';
import type { Task } from './components/TaskCard';
// StatusBar removed — redundant connection info
import { OrchestratorHealthBar } from './components/OrchestratorHealthBar';
import { VersionMismatchBanner } from './components/VersionMismatchBanner';
import { NullBranchDiagnosticsPanel } from './components/NullBranchDiagnosticsPanel';
import { parseTimestamp } from './utils/time';
import { matchesStatusFilter } from './utils/statusFilter';

const COLUMNS: KanbanColumn[] = [
  { id: 'QUEUED', label: 'Queued', includes: ['QUEUED', 'PROVISIONING'] },
  { id: 'IN_PROGRESS', label: 'In Progress', includes: ['IN_PROGRESS'] },
  { id: 'EVALUATING', label: 'Evaluating', includes: ['EVALUATING'] },
  { id: 'BLOCKED', label: 'Blocked', includes: ['BLOCKED'] },
  { id: 'PARKED', label: 'Parked', includes: ['PARKED'] },
  { id: 'FAILED', label: 'Failed', includes: ['FAILED'] },
  { id: 'CANCELLED', label: 'Cancelled', includes: ['CANCELLED'] },
  { id: 'COMPLETED', label: 'Completed', includes: ['COMPLETED'] },
];

const SORT_KEY = 'blueprint-admin:kanban-sort';
const CANCELLABLE_STATUSES = new Set(['QUEUED', 'IN_PROGRESS', 'PROVISIONING', 'EVALUATING', 'BLOCKED', 'PARKED']);
const TERMINAL_STATUSES = new Set(['COMPLETED', 'CANCELLED', 'FAILED']);



export default function App() {
  const s = useAppState();
  const searchInputRef = useRef<HTMLInputElement>(null);
  const [drawerWidth, setDrawerWidth] = useState(0);
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
  useEffect(() => {
    const onFailureClassFilter = (e: Event) => {
      const failureClass = (e as CustomEvent<string>).detail;
      if (failureClass && failureClass in FAILURE_CLASS_CONFIG) s.setFailureClassFilter([failureClass]);
    };
    window.addEventListener('blueprint:failure-class-filter', onFailureClassFilter);
    return () => window.removeEventListener('blueprint:failure-class-filter', onFailureClassFilter);
  }, [s.setFailureClassFilter]);

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

  // Apply a saved view's snapshot through the live filter/sort setters so the
  // active-filter persistence layer keeps working exactly as before. Destructure
  // the (stable) setters so exhaustive-deps can track them without depending on
  // the whole per-render state object `s`.
  const {
    setSearchQuery, setTaskTypeFilter, setStatusFilter, setDateRange,
    setBlockedFilter, setRequiresApproval, setStuckFilter, setStuckMinSeconds,
    setFailureClassFilter,
  } = s;
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
  const showMessages = s.showMessages;

  const [directions, setDirections] = useState<{ id: string; text: string }[]>([]);
  useEffect(() => {
    if (!selectedProjectId) { setDirections([]); return; }
    const pid = selectedProjectId;
    apiFetch(`${API_BASE_URL}/api/admin/projects/${encodeURIComponent(pid)}/directions`)
      .then(res => res.json())
      .then((data: unknown) => {
        if (pid !== selectedProjectId) return;
        const list = Array.isArray(data) ? data : ((data as Record<string, unknown>)?.directions ?? []);
        setDirections((list as { id: string; text?: string; title?: string }[]).map(d => ({ id: d.id, text: d.text ?? d.title ?? d.id })));
      })
      .catch(() => { /* directions are non-critical, silently ignore */ });
  }, [selectedProjectId]);

  // Clear message count when panel opens
  useEffect(() => {
    if (showMessages) {
      setOpenMessageCount(0);
      setNewSinceLastVisit(0);
    }
  }, [showMessages]);

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
    if (showMessages || !selectedProjectId) return;
    closedPanelNoisePrefsRef.current = loadEventPrefs(s.user?.email, selectedProjectId);
    closedPanelTempMutesRef.current = loadTemporaryMutes(s.user?.email, selectedProjectId);
  }, [showMessages, selectedProjectId, s.user?.email]);
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
    enabled: !!selectedProjectId && !showMessages,
    deps: [selectedProjectId, showMessages, s.user?.email],
  });

  useEffect(() => {
    function handleSearchShortcut(e: KeyboardEvent) {
      if (e.key !== '/') return;
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      if (s.showNewTaskModal || s.logTask || s.showResearchBrowser || s.showSettings) return;
      e.preventDefault();
      searchInputRef.current?.focus();
    }
    document.addEventListener('keydown', handleSearchShortcut);
    return () => document.removeEventListener('keydown', handleSearchShortcut);
  }, [s.showNewTaskModal, s.logTask, s.showResearchBrowser, s.showSettings]);

  // New project modal
  const [showNewProjectModal, setShowNewProjectModal] = useState(false);
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
    setSettingsInitialSection('integration-branch');
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

  const pendingFindingsCount = useMemo(() => {
    const implemented = new Set<string>();
    for (const t of s.tasks) {
      const ctx = t.context as Record<string, unknown> | undefined;
      if (ctx?.research_task_id && ctx?.finding_index !== undefined) {
        implemented.add(`${ctx.research_task_id}-${ctx.finding_index}`);
      }
    }
    let count = 0;
    for (const t of s.tasks) {
      if (t.task_type !== 'research' || t.status !== 'COMPLETED' || !t.research_findings) continue;
      const arr = Array.isArray(t.research_findings) ? t.research_findings : [];
      arr.forEach((_: unknown, i: number) => {
        if (!implemented.has(`${t.id}-${i}`)) count++;
      });
    }
    return count;
  }, [s.tasks]);

  const runningTasks = useMemo(() => s.tasks.filter(t => t.status === 'IN_PROGRESS' || t.status === 'PROVISIONING'), [s.tasks]);
  const queuedTasks = useMemo(() => s.tasks.filter(t => t.status === 'QUEUED'), [s.tasks]);

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

  const hasActiveFilters = s.tasks.length > 0 && (filteredTasks.length < s.tasks.length || s.failureClassFilter.length > 0);

  const [showAnalytics, setShowAnalytics] = useState(false);

  // activeTab drives the primary nav; 'home' renders ConsoleHome, 'board' renders
  // the kanban/pipeline views, 'directions' renders DirectionsPanel in page mode.
  const activeTab: PrimaryTab = s.activeTab;
  const handleTabChange = (tab: PrimaryTab) => {
    if (tab === 'settings') {
      s.handleToggleSettings();
      return;
    }
    s.setActiveTab(tab);
    if (tab === 'board' && s.viewMode === 'home') {
      s.setViewMode('kanban');
    }
  };

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
    <div className="flex flex-col bg-neutral-950 text-neutral-200 font-sans p-3 md:p-6 min-h-screen md:h-screen md:overflow-hidden">
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
          if (mode === 'home') s.setActiveTab('home');
          else s.setActiveTab('board');
        }}
        showResearchBrowser={s.showResearchBrowser}
        onToggleResearchBrowser={() => s.setShowResearchBrowser(prev => !prev)}
        showKnowledgeBrowser={s.showKnowledgeBrowser}
        onToggleKnowledgeBrowser={() => s.setShowKnowledgeBrowser(prev => !prev)}
        showArchive={s.showArchive}
        onToggleArchive={() => s.setShowArchive(!s.showArchive)}
        showMessages={s.showMessages || s.showComposeDrawer}
        onToggleMessages={() => {
          if (s.showMessages) { s.setShowMessages(false); }
          else { s.setShowComposeDrawer(prev => !prev); }
        }}
        alertCount={combinedAlerts.length}
        alertMaxSeverity={alertMaxSeverity}
        showAlertsPopover={s.showAlertsPopover}
        onToggleAlertsPopover={() => s.setShowAlertsPopover(prev => !prev)}
        onOpenActivityFeed={() => setShowActivityFeed(true)}
        openMessageCount={openMessageCount}
        newSinceLastVisitCount={newSinceLastVisit}
        needsAttentionCount={s.needsAttentionCount}
        onOpenNewTaskModal={s.openNewTaskModal}
        onShowDirections={() => s.setActiveTab('directions')}
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
        showMetrics={s.showMetrics}
        onToggleMetrics={() => s.setShowMetrics(prev => !prev)}
        metricsHistory={s.metricsHistory}
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
      />

      <div
        className="overflow-hidden transition-all duration-300 ease-in-out shrink-0"
        style={{ maxHeight: s.showMetrics ? '700px' : '0px', opacity: s.showMetrics ? 1 : 0 }}
      >
        <ErrorBoundary level="panel">
          <MetricsDashboard
            metricsHistory={s.metricsHistory}
            cumulativeCost={s.cumulativeCost}
            cumulativeVmHours={s.cumulativeVmHours}
            maxConcurrentVMs={s.maxConcurrentVMs}
            tasks={s.tasks}
            metricsWindow={s.metricsWindow}
            onWindowChange={s.setMetricsWindow}
            projectId={s.selectedProjectId ?? undefined}
          />
        </ErrorBoundary>
      </div>

      <OrchestratorHealthBar
        health={s.orchestratorHealth}
        detailedHealth={s.detailedHealth}
        apiHealthy={s.apiHealthy}
        wsConnected={s.wsConnected}
        lastFetchTime={s.lastFetchTime}
        tasks={s.tasks}
        pipelineThrottle={s.pipelineThrottle}
      />

      {/* Primary nav: project switcher + tab strip */}
      <div className="flex items-center gap-3 shrink-0 border-b border-neutral-800/60 pb-1 mb-1">
        <ProjectSwitcherDropdown
          projects={s.projects}
          selectedProjectId={s.selectedProjectId}
          onSelectProject={s.setSelectedProjectId}
        />
        <nav className="flex items-center gap-0.5 flex-1">
          {(['home', 'board', 'directions', 'settings'] as const).map(tab => {
            const isActive = tab === 'settings' ? s.showSettings : activeTab === tab;
            const label = tab.charAt(0).toUpperCase() + tab.slice(1);
            return (
              <button
                key={tab}
                onClick={() => handleTabChange(tab)}
                className={`px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${
                  isActive
                    ? 'bg-neutral-800 text-neutral-100'
                    : 'text-neutral-500 hover:text-neutral-300 hover:bg-neutral-800/50'
                }`}
              >
                {label}
              </button>
            );
          })}
        </nav>
        {/* eslint-disable-next-line viewport/min-touch-target */}
        <button
          onClick={() => setShowNewProjectModal(true)}
          className="text-xs text-neutral-600 hover:text-neutral-400 transition-colors px-2 py-1"
          title="New project"
        >
          + Project
        </button>
      </div>

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

      {s.showUserManagement && s.userRole === 'admin' ? (
        <UserManagement
          users={s.allowedUsers}
          currentUserEmail={s.user?.email || ''}
          onAdd={s.addAllowedUser}
          onUpdateRole={s.updateAllowedUserRole}
          onRemove={s.removeAllowedUser}
          onClose={() => s.setShowUserManagement(false)}
        />
      ) : s.showMessages ? (
        <MessagesPanel
          selectedProjectId={s.selectedProjectId}
          githubRepo={selectedProject ? `${selectedProject.github_owner}/${selectedProject.github_repo}` : undefined}
          defaultBranch={s.promotionTargetData?.promotion_target || s.integrationBranchData?.integration_branch || undefined}
          onClose={() => s.setShowMessages(false)}
          addToast={s.addToast}
          onMessageCountChange={handleMessageCountChange}
          onOpenTask={handleViewTask}
          notifyOnHighSignalMessage={s.notifyOnHighSignalMessage}
          strategistActive={s.strategistActive}
          cycleProgress={s.cycleProgress}
          lastCycleCompletion={s.lastCycleCompletion}
        />
      ) : s.showArchive ? (
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
      ) : activeTab === 'directions' ? (
        <ErrorBoundary level="panel">
          <DirectionsPanel
            pageMode
            addToast={s.addToast}
            selectedProjectId={s.selectedProjectId}
            projects={s.projects}
            fetchDirectionsRef={s.fetchDirectionsRef}
            strategistActive={s.strategistActive}
            cycleProgress={s.cycleProgress}
            onViewFindings={(dirId) => { setResearchBrowserDirectionId(dirId); s.setShowResearchBrowser(true); }}
            onNavigateToTask={(taskId) => {
              const target = s.tasks.find(t => t.id === taskId);
              if (target) s.handleViewLogs(target);
              else fetchTask(taskId).then(s.handleViewLogs).catch(() => { s.addToast('Could not load task — it may have been deleted', 'error'); });
            }}
          />
        </ErrorBoundary>
      ) : activeTab === 'home' ? (
        <ErrorBoundary level="panel">
          <ConsoleHome
            selectedProjectId={s.selectedProjectId}
            scopedActiveCount={s.scopedActiveCount}
            scopedQueuedCount={s.scopedQueuedCount}
            globalActiveCount={s.globalActiveCount}
            globalQueuedCount={s.globalQueuedCount}
            tasks={s.tasks}
            onViewTask={s.handleViewLogs}
            onShowAnalytics={() => setShowAnalytics(true)}
            onOpenFleet={() => s.setShowFleetDetail(true)}
          />
        </ErrorBoundary>
      ) : (
        <>
          {/* Board view mode sub-tabs + Research/Knowledge toolbar buttons */}
          <div className="flex items-center gap-1 shrink-0 mb-1">
            {(['kanban', 'pipeline'] as const).map(mode => (
              <button
                key={mode}
                onClick={() => s.setViewMode(mode)}
                className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
                  s.viewMode === mode
                    ? 'bg-neutral-800 text-neutral-100'
                    : 'text-neutral-500 hover:text-neutral-300 hover:bg-neutral-800/50'
                }`}
              >
                {mode.charAt(0).toUpperCase() + mode.slice(1)}
              </button>
            ))}
            <div className="flex-1" />
            {/* eslint-disable-next-line viewport/min-touch-target */}
            <button
              onClick={() => s.setShowResearchBrowser(prev => !prev)}
              className={`flex items-center gap-1 px-2 py-1 rounded text-xs transition-colors relative ${
                s.showResearchBrowser ? 'text-cyan-400 bg-cyan-500/10' : 'text-neutral-500 hover:text-neutral-300 hover:bg-neutral-800/50'
              }`}
              title="Research Findings"
            >
              <Microscope className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">Research</span>
              {pendingFindingsCount > 0 && (
                <span className="ml-0.5 bg-cyan-500 text-black text-[8px] font-bold w-4 h-4 rounded-full leading-none flex items-center justify-center">
                  {pendingFindingsCount}
                </span>
              )}
            </button>
            {/* eslint-disable-next-line viewport/min-touch-target */}
            <button
              onClick={() => s.setShowKnowledgeBrowser(prev => !prev)}
              className={`flex items-center gap-1 px-2 py-1 rounded text-xs transition-colors ${
                s.showKnowledgeBrowser ? 'text-violet-400 bg-violet-500/10' : 'text-neutral-500 hover:text-neutral-300 hover:bg-neutral-800/50'
              }`}
              title="Knowledge Library"
            >
              <BookOpen className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">Knowledge</span>
            </button>
          </div>

          <div className="flex items-center gap-2 shrink-0 mb-1 flex-wrap">
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
              onClick={() => s.setCompactMode(v => !v)}
              title={s.compactMode ? 'Switch to normal density' : 'Switch to compact density'}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border transition-colors ${
                s.compactMode
                  ? 'bg-amber-500/20 border-amber-500/40 text-amber-400'
                  : 'bg-neutral-900/50 border-neutral-800/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300'
              }`}
            >
              <LayoutList className="w-3.5 h-3.5" />
              {s.compactMode ? 'Compact' : 'Normal'}
            </button>
            )}
            {s.viewMode === 'kanban' && (
            <button
              onClick={s.toggleSelectionMode}
              title={s.selectionMode ? 'Exit selection mode' : 'Select tasks for bulk actions'}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border transition-colors ${
                s.selectionMode
                  ? 'bg-amber-500/20 border-amber-500/40 text-amber-400'
                  : 'bg-neutral-900/50 border-neutral-800/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300'
              }`}
            >
              <CheckSquare className="w-3.5 h-3.5" />
              {s.selectionMode ? 'Selecting' : 'Select'}
            </button>
            )}
            <div className="flex-1" />
            <span className="text-xs text-neutral-500">{filteredTasks.length} of {s.tasks.length}</span>
          </div>

          {/* Named saved filter views */}
          <BoardSavedViewsBar
            key={`${s.user?.email ?? 'anon'}:${s.selectedProjectId ?? 'none'}`}
            userId={s.user?.email}
            projectId={s.selectedProjectId}
            current={boardFilterSnapshot}
            onApply={applyBoardView}
          />

          {/* Quick-filter preset buttons */}
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

          {s.viewMode === 'kanban' ? (
            <ErrorBoundary level="panel">
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
            </ErrorBoundary>
          ) : s.viewMode === 'pipeline' ? (
            <ErrorBoundary level="panel">
            <main className="flex-1 overflow-hidden flex flex-col">
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
          ) : null}

          <Pagination page={s.page} onPageChange={s.setPage} taskCount={filteredTasks.length} serverTaskCount={s.tasks.length} limit={s.limit} onLimitChange={s.setLimit} totalTasks={s.totalTasks} />
        </>
      )}

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
          onClose={() => s.setShowKnowledgeBrowser(false)}
          onLaunchWebResearchTask={s.openWebResearchTaskModal}
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

      {showAnalytics && (
        <div className="fixed inset-0 z-[60] bg-black/60 flex items-start justify-center overflow-y-auto p-4" onClick={(e) => { if (e.target === e.currentTarget) setShowAnalytics(false); }}>
          <div className="relative w-full max-w-6xl bg-neutral-950 rounded-xl border border-neutral-800 shadow-2xl my-8">
            <div className="flex items-center justify-between px-6 py-4 border-b border-neutral-800">
              <h2 className="text-sm font-semibold text-neutral-200">Detailed Analytics</h2>
              {/* eslint-disable-next-line viewport/min-touch-target */}
              <button onClick={() => setShowAnalytics(false)} className="text-neutral-500 hover:text-neutral-300 transition-colors text-xs px-2 py-1">Close</button>
            </div>
            <div className="p-4">
              <ErrorBoundary level="panel">
                <DashboardHome tasks={s.tasks} selectedProjectId={s.selectedProjectId} projects={s.projects} globalActiveCount={s.globalActiveCount} globalQueuedCount={s.globalQueuedCount} scopedActiveCount={s.scopedActiveCount} scopedQueuedCount={s.scopedQueuedCount} onViewTask={s.handleViewLogs} onDirectionClick={(dirId) => { setResearchBrowserDirectionId(dirId); s.setShowResearchBrowser(true); }} onConfigureTargets={handleConfigureTargets} onSpawnOrphanFix={(pr) => s.openPrefilledTaskModal({ taskType: 'fix', title: pr.pr_title, prompt: `PR #${pr.pr_number} is open but has no active task — investigate and either close or push remediation. PR: ${pr.pr_url}`, prNumber: pr.pr_number, prBranch: pr.pr_branch })} onPanelReviewPr={(pr) => s.openPrefilledTaskModal({ taskType: 'panel_review', title: `Panel review PR #${pr.pr_number}`, prompt: '', prNumber: pr.pr_number, prBranch: pr.pr_branch, baseBranch: pr.pr_base_branch })} userRole={s.userRole} />
              </ErrorBoundary>
            </div>
          </div>
        </div>
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

      {s.showComposeDrawer && (
        <ComposeDrawer
          projectId={s.selectedProjectId}
          userEmail={s.user?.email ?? ''}
          onOpenFullPanel={() => {
            s.setShowComposeDrawer(false);
            s.setShowMessages(true);
          }}
          onClose={() => s.setShowComposeDrawer(false)}
          addToast={s.addToast}
          strategistActive={s.strategistActive}
        />
      )}

      {s.showFleetDetail && (
        <FleetDetailDrawer
          onClose={() => s.setShowFleetDetail(false)}
          onViewTask={handleViewTask}
          projects={s.projects}
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
