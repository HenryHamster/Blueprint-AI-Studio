import { useMemo, useRef, useEffect, useState, useCallback } from 'react';
import { ErrorBoundary } from './components/ErrorBoundary';
import { Activity, AlertCircle, LayoutList, CheckSquare } from 'lucide-react';
import { ColumnsPicker } from './components/ColumnsPicker';
import type { ColumnPref } from './components/ColumnsPicker';
import { ConfirmDialog } from './components/ConfirmDialog';
import { useAppState } from './useAppState';
import { DirectionsPanel } from './components/DirectionsPanel';
import TaskStreamPanel from './components/TaskStreamPanel';
import { ResearchBrowser } from './components/ResearchBrowser';
import { PipelineView } from './components/PipelineView';
import { ProjectDashboard } from './components/ProjectDashboard';
import { auth, signInWithGoogle, signOut } from './firebase';
import { fetchTask, apiFetch, API_BASE_URL, fetchSrMissedRetrospectives } from './api';
import { SrMissedRetrospective, type SrMissedTask, type SrInvestigationSignal } from './components/SrTrendBreakdown';
import { TaskDetailDrawer } from './components/TaskDetailDrawer';
import { ToastContainer } from './components/Toasts';
import { AlertsBanner } from './components/AlertsBanner';
import { NewTaskModal } from './components/NewTaskModal';
import { NewProjectModal } from './components/NewProjectModal';
import { HeaderBar } from './components/HeaderBar';
import type { SectionId } from './components/SettingsDropdown';
import { TaskLaunchFAB } from './components/TaskLaunchFAB';
import MetricsDashboard from './components/MetricsDashboard';
import { SearchFilterBar, SingleSelectDropdown } from './components/SearchFilterBar';
import { DashboardHome } from './components/DashboardHome';
import { BulkActionBar } from './components/BulkActionBar';
import { KanbanBoard } from './components/KanbanBoard';
import type { KanbanColumn, SortBy } from './components/KanbanBoard';
import { Pagination } from './components/Pagination';
import ArchiveView from './components/ArchiveView';
import { UserManagement } from './components/UserManagement';
import { ProjectMembers } from './components/ProjectMembers';
import { CatchUpPanel } from './components/CatchUpPanel';
import { MessagesPanel } from './components/MessagesPanel';
import { QuickFilterPresets } from './components/QuickFilterPresets';
// StatusBar removed — redundant connection info
import { OrchestratorHealthBar } from './components/OrchestratorHealthBar';
import { VersionMismatchBanner } from './components/VersionMismatchBanner';
import { DiagnosticsPanel } from './components/DiagnosticsPanel';
import { PlaybookPanel } from './components/PlaybookPanel';
import { WarmVmPanel } from './components/WarmVmPanel';
import { FILTER_TASK_TYPE_IDS } from './taskCapabilities';
import { parseTimestamp } from './utils/time';

const COLUMNS: KanbanColumn[] = [
  { id: 'QUEUED', label: 'Queued', includes: ['QUEUED', 'PROVISIONING'] },
  { id: 'IN_PROGRESS', label: 'In Progress', includes: ['IN_PROGRESS'] },
  { id: 'EVALUATING', label: 'Evaluating', includes: ['EVALUATING'] },
  { id: 'FAILED', label: 'Failed', includes: ['FAILED'] },
  { id: 'CANCELLED', label: 'Cancelled', includes: ['CANCELLED'] },
  { id: 'COMPLETED', label: 'Completed', includes: ['COMPLETED'] },
];

const SORT_KEY = 'blueprint-admin:kanban-sort';
const CANCELLABLE_STATUSES = new Set(['QUEUED', 'IN_PROGRESS', 'PROVISIONING', 'EVALUATING']);
const TERMINAL_STATUSES = new Set(['COMPLETED', 'CANCELLED', 'FAILED']);



export default function App() {
  const s = useAppState();
  const searchInputRef = useRef<HTMLInputElement>(null);
  const [drawerWidth, setDrawerWidth] = useState(0);
  const [openMessageCount, setOpenMessageCount] = useState(0);
  const handleMessageCountChange = useCallback((count: number) => setOpenMessageCount(count), []);
  const [sortBy, setSortBy] = useState<SortBy>(() => {
    try {
      const stored = localStorage.getItem(SORT_KEY);
      return (stored as SortBy) ?? 'newest';
    } catch { return 'newest'; }
  });

  useEffect(() => {
    localStorage.setItem(SORT_KEY, sortBy);
  }, [sortBy]);

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

  // Clear message count when panel opens
  useEffect(() => {
    if (showMessages) {
      setOpenMessageCount(0);
    }
  }, [showMessages]);
  useEffect(() => {
    if (!selectedProjectId || showMessages) return;
    const pid = selectedProjectId; // narrowed to string
    let cancelled = false;
    async function fetchCount() {
      try {
        const params = new URLSearchParams({ project_id: pid, status: 'open', limit: '100' });
        const res = await apiFetch(`${API_BASE_URL}/api/admin/messages?${params}`);
        if (!res.ok || cancelled) return;
        const data = await res.json();
        const msgs: { from_type?: string }[] = data.messages ?? [];
        if (!cancelled) setOpenMessageCount(msgs.filter(m => (m.from_type || '').toLowerCase() !== 'user').length);
      } catch { /* ignore */ }
    }
    fetchCount();
    const interval = setInterval(fetchCount, 30_000);
    return () => { cancelled = true; clearInterval(interval); };
  }, [selectedProjectId, showMessages]);

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

  const [showSrMissed, setShowSrMissed] = useState(false);
  const [srMissedTasks, setSrMissedTasks] = useState<SrMissedTask[]>([]);
  const [srMissedLoading, setSrMissedLoading] = useState(false);
  const [srMissedCategory, setSrMissedCategory] = useState('');
  const srMissedRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!showSrMissed || !selectedProjectId) return;
    setSrMissedLoading(true);
    fetchSrMissedRetrospectives(selectedProjectId, { days: 14, page_size: 200 })
      .then(r => setSrMissedTasks(r.items))
      .catch(() => {})
      .finally(() => setSrMissedLoading(false));
  }, [showSrMissed, selectedProjectId]);

  // Open operator-spawned SR investigations, keyed by category — used to dedup spawning.
  const [srInvestigationTaskIds, setSrInvestigationTaskIds] = useState<Record<string, string>>({});
  useEffect(() => {
    if (!selectedProjectId) { setSrInvestigationTaskIds({}); return; }
    const pid = selectedProjectId;
    let cancelled = false;
    async function fetchIds() {
      try {
        const res = await apiFetch(`${API_BASE_URL}/api/admin/projects/${encodeURIComponent(pid)}/sr-investigation-task-ids`);
        if (!res.ok || cancelled) return;
        const data = (await res.json()) as { task_ids?: Record<string, string> };
        if (!cancelled) setSrInvestigationTaskIds(data.task_ids ?? {});
      } catch { /* ignore */ }
    }
    fetchIds();
    const interval = setInterval(fetchIds, 30_000);
    return () => { cancelled = true; clearInterval(interval); };
  }, [selectedProjectId]);

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
      const knownTypes: readonly string[] = FILTER_TASK_TYPE_IDS;
      const typeMatch = s.taskTypeFilter.includes(taskType) || (!knownTypes.includes(taskType) && s.taskTypeFilter.length === knownTypes.length);
      const statusMatch = s.statusFilter.includes(t.status);
      const query = s.searchQuery.toLowerCase();
      const searchMatch = !query || t.id.toLowerCase().includes(query) || (t.title?.toLowerCase().includes(query)) || (t.description?.toLowerCase().includes(query)) || (t.latest_status_message?.toLowerCase().includes(query));
      const projectMatch = s.selectedProjectId === null || t.project_id === s.selectedProjectId;
      const dateMatch = cutoff === null || parseTimestamp(t.created_at).getTime() >= cutoff;
      const blockedMatch = !s.blockedFilter || (t.status === 'QUEUED' && t.depends_on != null && t.depends_on.length > 0);
      const approvalMatch = !s.requiresApproval || t.requires_approval;
      const stuckMatch = !s.stuckFilter || stuckTaskIds.has(t.id);
      return typeMatch && statusMatch && searchMatch && projectMatch && dateMatch && blockedMatch && approvalMatch && stuckMatch;
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [s.tasks, s.taskTypeFilter, s.statusFilter, s.searchQuery, s.selectedProjectId, s.dateRange, s.blockedFilter, s.requiresApproval, s.stuckFilter, s.stuckMinSeconds, stuckTaskIds]);

  const hasActiveFilters = s.tasks.length > 0 && filteredTasks.length < s.tasks.length;

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
        onSetViewMode={s.setViewMode}
        showResearchBrowser={s.showResearchBrowser}
        onToggleResearchBrowser={() => s.setShowResearchBrowser(prev => !prev)}
        showArchive={s.showArchive}
        onToggleArchive={() => s.setShowArchive(!s.showArchive)}
        showMessages={s.showMessages}
        onToggleMessages={() => s.setShowMessages(prev => !prev)}
        openMessageCount={openMessageCount}
        needsAttentionCount={s.needsAttentionCount}
        onOpenNewTaskModal={s.openNewTaskModal}
        onShowDirections={() => s.setShowDirections(true)}
        onSignOut={() => signOut(auth)}
        userEmail={s.user.email || ''}
        wsRef={s.wsRef}
        showShortcutHelp={s.showShortcutHelp}
        onToggleShortcutHelp={() => s.setShowShortcutHelp(prev => !prev)}
        shortcutHelpRef={s.shortcutHelpRef}
        showSettings={s.showSettings}
        onToggleSettings={s.handleToggleSettings}
        settingsRef={s.settingsRef}
        settingsInitialSection={settingsInitialSection}
        maxRetries={s.maxRetries}
        maxAutonomousTasksPerHour={s.maxAutonomousTasksPerHour}
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

      <ProjectDashboard
        projects={s.projects}
        tasks={s.tasks}
        selectedProjectId={s.selectedProjectId}
        onSelectProject={s.setSelectedProjectId}
        userRole={s.userRole}
        onNewProject={() => setShowNewProjectModal(true)}
        onOpenSetupGuide={(projectId) => setSetupProjectId(projectId)}
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

      <AlertsBanner
        alerts={[...zoneHealthAlerts, ...s.alerts]}
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
      />

      {!s.showArchive && !s.showUserManagement && (
        <CatchUpPanel
          selectedProjectId={s.selectedProjectId}
          projects={s.projects}
          lastVisitTimestamp={s.lastVisitTimestamp}
          refreshInterval={s.refreshInterval}
          needsAttentionCount={s.needsAttentionCount}
          onSelectTask={(task) => {
            fetchTask(task.id).then(s.handleViewLogs).catch(() => {
              s.addToast('Could not load task — it may have been deleted', 'error');
            });
          }}
          onApprove={(taskId) => s.handleApprove(taskId)}
          onRetry={(taskId) => s.handleRetry(taskId)}
          onBulkApprove={(taskIds) => s.handleBulkAction('approve', taskIds)}
          onBulkRetry={(taskIds) => s.handleBulkAction('retry', taskIds)}
          wsRef={s.wsRef}
        />
      )}

      <DiagnosticsPanel
        diagnostics={s.diagnostics}
        onViewTask={handleViewTask}
      />

      <WarmVmPanel onViewTask={handleViewTask} />


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
          onClose={() => s.setShowMessages(false)}
          addToast={s.addToast}
          onMessageCountChange={handleMessageCountChange}
          onOpenTask={handleViewTask}
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
        />
      ) : (
        <>
          {s.viewMode !== 'home' && <div className="flex items-center gap-2 shrink-0 mb-1 flex-wrap">
            <SearchFilterBar
              searchQuery={s.searchQuery}
              onSearchChange={s.setSearchQuery}
              taskTypeFilter={s.taskTypeFilter}
              onTypeFilterChange={s.setTaskTypeFilter}
              statusFilter={s.statusFilter}
              onStatusFilterChange={s.setStatusFilter}
              dateRange={s.dateRange}
              onDateRangeChange={s.setDateRange}
              blockedFilter={s.blockedFilter}
              onBlockedFilterChange={s.setBlockedFilter}
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
          </div>}

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
            setStatusFilter={s.setStatusFilter}
            setTaskTypeFilter={s.setTaskTypeFilter}
            setDateRange={s.setDateRange}
            setRequiresApproval={s.setRequiresApproval}
            setBlockedFilter={s.setBlockedFilter}
            setStuckFilter={s.setStuckFilter}
            setStuckMinSeconds={s.setStuckMinSeconds}
            setSearchQuery={s.setSearchQuery}
          />

          {s.viewMode === 'home' ? (
            <>
              <ErrorBoundary level="panel"><DashboardHome tasks={s.tasks} selectedProjectId={s.selectedProjectId} projects={s.projects} onViewTask={s.handleViewLogs} onDirectionClick={(dirId) => { setResearchBrowserDirectionId(dirId); s.setShowResearchBrowser(true); }} onCategoryClick={(cat) => { setSrMissedCategory(cat); setShowSrMissed(true); setTimeout(() => srMissedRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 50); }} onConfigureTargets={handleConfigureTargets} /></ErrorBoundary>
              <PlaybookPanel selectedProjectId={s.selectedProjectId} />
              {s.selectedProjectId && (
                <div ref={srMissedRef} className="shrink-0 mt-2 bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3">
                  <button
                    onClick={() => { setShowSrMissed(v => { if (v) setSrMissedCategory(''); return !v; }); }}
                    className="text-xs font-medium text-neutral-400 hover:text-neutral-200"
                  >
                    Self-Review Missed Retrospectives {showSrMissed ? '▲' : '▼'}
                  </button>
                  {showSrMissed && (
                    srMissedLoading
                      ? <p className="text-[10px] text-neutral-500 mt-2">Loading…</p>
                      : <div className="mt-2"><SrMissedRetrospective tasks={srMissedTasks} onTaskClick={handleViewTask} initialCategory={srMissedCategory} /></div>
                  )}
                </div>
              )}
            </>
          ) : s.viewMode === 'kanban' ? (
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
              searchQuery={s.searchQuery}
              onDirectionClick={(dirId) => { setResearchBrowserDirectionId(dirId); s.setShowResearchBrowser(true); }}
            />
            </ErrorBoundary>
          ) : (
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
          )}

          {s.viewMode !== 'home' && <Pagination page={s.page} onPageChange={s.setPage} taskCount={filteredTasks.length} serverTaskCount={s.tasks.length} limit={s.limit} onLimitChange={s.setLimit} totalTasks={s.totalTasks} />}
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

      {s.showDirections && (
        <DirectionsPanel
          onClose={() => s.setShowDirections(false)}
          addToast={s.addToast}
          selectedProjectId={s.selectedProjectId}
          projects={s.projects}
          onViewFindings={(dirId) => { setResearchBrowserDirectionId(dirId); s.setShowResearchBrowser(true); }}
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

      <ToastContainer toasts={s.toasts} onDismiss={s.dismissToast} />

      <TaskLaunchFAB
        onClick={s.openNewTaskModal}
        visible={!s.showNewTaskModal && !s.logTask && !showNewProjectModal && !s.showMessages}
      />

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
    </ErrorBoundary>
  );
}
