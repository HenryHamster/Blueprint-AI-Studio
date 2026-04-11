import { useMemo, useRef, useEffect, useState } from 'react';
import { ErrorBoundary } from './components/ErrorBoundary';
import { Activity, AlertCircle, Archive, CheckSquare, LayoutList, Layers, LayoutGrid, ChevronUp, ChevronDown, UserPlus } from 'lucide-react';
import { ConfirmDialog } from './components/ConfirmDialog';
import { useAppState } from './useAppState';
import { DirectionsPanel } from './components/DirectionsPanel';
import TaskStreamPanel from './components/TaskStreamPanel';
import { ResearchBrowser } from './components/ResearchBrowser';
import { PipelineView } from './components/PipelineView';
import { ProjectDashboard } from './components/ProjectDashboard';
import { auth, signInWithGoogle, signOut } from './firebase';
import { fetchTask } from './api';
import { TaskDetailDrawer } from './components/TaskDetailDrawer';
import { ToastContainer } from './components/Toasts';
import { AlertsBanner } from './components/AlertsBanner';
import { NewTaskModal } from './components/NewTaskModal';
import { HeaderBar } from './components/HeaderBar';
import MetricsDashboard from './components/MetricsDashboard';
import { SearchFilterBar } from './components/SearchFilterBar';
import { KanbanBoard } from './components/KanbanBoard';
import type { KanbanColumn, SortBy } from './components/KanbanBoard';
import { Pagination } from './components/Pagination';
import ArchiveView from './components/ArchiveView';
import ActivityFeed from './components/ActivityFeed';
import { UserManagement } from './components/UserManagement';
import { ProjectMembers } from './components/ProjectMembers';
import { OrchestratorHealthBar } from './components/OrchestratorHealthBar';
import { CatchUpPanel } from './components/CatchUpPanel';
import { DiagnosticsPanel } from './components/DiagnosticsPanel';
import { TimelineView } from './components/TimelineView';
import { MessagesPanel } from './components/MessagesPanel';

const COLUMNS: KanbanColumn[] = [
  { id: 'QUEUED', label: 'Queued', includes: ['QUEUED', 'PROVISIONING'] },
  { id: 'IN_PROGRESS', label: 'In Progress', includes: ['IN_PROGRESS'] },
  { id: 'EVALUATING', label: 'Evaluating', includes: ['EVALUATING'] },
  { id: 'FAILED', label: 'Failed', includes: ['FAILED'] },
  { id: 'CANCELLED', label: 'Cancelled', includes: ['CANCELLED'] },
  { id: 'COMPLETED', label: 'Completed', includes: ['COMPLETED'] },
];

const SORT_KEY = 'blueprint-admin:kanban-sort';

interface ColumnPref { id: string; visible: boolean; }

function ColumnsPicker({ prefs, onPrefsChange }: { prefs: ColumnPref[]; onPrefsChange: (p: ColumnPref[]) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [open]);

  function toggle(id: string) {
    onPrefsChange(prefs.map(p => p.id === id ? { ...p, visible: !p.visible } : p));
  }

  function move(id: string, dir: -1 | 1) {
    const idx = prefs.findIndex(p => p.id === id);
    if (idx < 0) return;
    const swapIdx = idx + dir;
    if (swapIdx < 0 || swapIdx >= prefs.length) return;
    const next = [...prefs];
    [next[idx], next[swapIdx]] = [next[swapIdx], next[idx]];
    onPrefsChange(next);
  }

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen(o => !o)}
        className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border transition-colors ${
          open
            ? 'bg-amber-500/20 border-amber-500/40 text-amber-400'
            : 'bg-neutral-900/50 border-neutral-800/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300'
        }`}
      >
        <LayoutGrid className="w-3.5 h-3.5" />
        Columns
      </button>
      {open && (
        <div className="absolute top-full mt-1.5 left-0 z-50 bg-neutral-900 border border-neutral-800 rounded-xl shadow-xl p-1.5 min-w-[180px]">
          {prefs.map((pref, idx) => {
            const col = COLUMNS.find(c => c.id === pref.id);
            if (!col) return null;
            return (
              <div key={pref.id} className="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-neutral-800/50 group">
                <input
                  type="checkbox"
                  checked={pref.visible}
                  onChange={() => toggle(pref.id)}
                  className="w-3.5 h-3.5 accent-amber-400 cursor-pointer flex-shrink-0"
                />
                <span className="flex-1 text-xs text-neutral-300">{col.label}</span>
                <div className="flex flex-col gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                  <button
                    onClick={() => move(pref.id, -1)}
                    disabled={idx === 0}
                    className="p-0.5 rounded text-neutral-500 hover:text-neutral-300 disabled:opacity-30 disabled:cursor-not-allowed"
                  >
                    <ChevronUp className="w-3 h-3" />
                  </button>
                  <button
                    onClick={() => move(pref.id, 1)}
                    disabled={idx === prefs.length - 1}
                    className="p-0.5 rounded text-neutral-500 hover:text-neutral-300 disabled:opacity-30 disabled:cursor-not-allowed"
                  >
                    <ChevronDown className="w-3 h-3" />
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function App() {
  const s = useAppState();
  const searchInputRef = useRef<HTMLInputElement>(null);
  const [drawerWidth, setDrawerWidth] = useState(0);
  const [sortBy, setSortBy] = useState<SortBy>(() => {
    try {
      const stored = localStorage.getItem(SORT_KEY);
      return (stored as SortBy) ?? 'newest';
    } catch { return 'newest'; }
  });

  useEffect(() => {
    localStorage.setItem(SORT_KEY, sortBy);
  }, [sortBy]);

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

  const { showNewTaskModal, logTask, showResearchBrowser, showSettings, setShowActivityFeed } = s;
  useEffect(() => {
    function handleActivityFeedShortcut(e: KeyboardEvent) {
      if (e.key !== 'a' && e.key !== 'A') return;
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      if (showNewTaskModal || logTask || showResearchBrowser || showSettings) return;
      setShowActivityFeed(prev => !prev);
    }
    document.addEventListener('keydown', handleActivityFeedShortcut);
    return () => document.removeEventListener('keydown', handleActivityFeedShortcut);
  }, [showNewTaskModal, logTask, showResearchBrowser, showSettings, setShowActivityFeed]);

  // Project members panel
  const [showProjectMembers, setShowProjectMembers] = useState(false);
  const selectedProject = useMemo(
    () => s.projects.find(p => p.id === s.selectedProjectId) ?? null,
    [s.projects, s.selectedProjectId],
  );

  // Fetch project members when panel opens or project changes
  useEffect(() => {
    if (showProjectMembers && s.selectedProjectId) {
      s.fetchProjectMembers(s.selectedProjectId);
    }
  }, [showProjectMembers, s.selectedProjectId, s.fetchProjectMembers]);

  const [activityEvents, setActivityEvents] = useState<import('./components/ActivityFeed').ActivityEvent[]>([]);
  const [activityFeedFilter, setActivityFeedFilter] = useState<string | undefined>();
  const newActivityCount = useMemo(() => {
    if (!s.lastActivityFeedOpen) return 0;
    return activityEvents.filter(e => new Date(e.timestamp).getTime() > s.lastActivityFeedOpen!).length;
  }, [activityEvents, s.lastActivityFeedOpen]);

  // Backend version mismatch detection
  const [versionMismatch, setVersionMismatch] = useState(false);
  useEffect(() => {
    const expectedSha = import.meta.env.VITE_EXPECTED_BACKEND_SHA;
    if (!expectedSha) return; // Dev mode or not configured
    fetch('/api/version')
      .then(r => r.json())
      .then(data => {
        if (data.sha && data.sha !== expectedSha) {
          setVersionMismatch(true);
        }
      })
      .catch(() => {}); // Silently ignore if backend unreachable
  }, []);

  const [dismissedZoneAlertIds, setDismissedZoneAlertIds] = useState<Set<string>>(new Set());

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
      .map(pref => COLUMNS.find(c => c.id === pref.id)!)
      .filter(Boolean)
      .filter(col => col.includes.some(status => s.statusFilter.includes(status)));
  }, [effectiveColumnPrefs, s.statusFilter]);

  const filteredTasks = useMemo(() => s.tasks.filter(t => {
    const taskType = t.task_type || (t.payloads?.[0] as Record<string, unknown>)?.type as string || 'create';
    const knownTypes = ['create', 'plan', 'research', 'review', 'fix', 'migrate', 'refactor'];
    const typeMatch = s.taskTypeFilter.includes(taskType) || (!knownTypes.includes(taskType) && s.taskTypeFilter.length === knownTypes.length);
    const statusMatch = s.statusFilter.includes(t.status);
    const query = s.searchQuery.toLowerCase();
    const searchMatch = !query || (t.title?.toLowerCase().includes(query)) || (t.description?.toLowerCase().includes(query));
    const projectMatch = s.selectedProjectId === null || t.project_id === s.selectedProjectId;
    return typeMatch && statusMatch && searchMatch && projectMatch;
  }), [s.tasks, s.taskTypeFilter, s.statusFilter, s.searchQuery, s.selectedProjectId]);

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
        showActivityFeed={s.showActivityFeed}
        onToggleActivityFeed={() => s.setShowActivityFeed(prev => !prev)}
        showMessages={s.showMessages}
        onToggleMessages={() => s.setShowMessages(prev => !prev)}
        newActivityCount={newActivityCount}
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
        maxRetries={s.maxRetries}
        maxAutonomousTasksPerHour={s.maxAutonomousTasksPerHour}
        onUpdateMaxVMs={s.updateMaxVMs}
        onUpdateMaxRetries={s.updateMaxRetries}
        onUpdateMaxAutonomousRate={s.handleUpdateMaxAutonomousRate}
        autonomousResearchIntervalMinutes={s.autonomousResearchIntervalMinutes}
        onUpdateAutonomousResearchInterval={s.handleUpdateAutonomousResearchInterval}
        directionCooldownMinutes={s.directionCooldownMinutes}
        onUpdateDirectionCooldownMinutes={s.handleUpdateDirectionCooldownMinutes}
        strategicResearchFrequency={s.strategicResearchFrequency}
        onUpdateStrategicResearchFrequency={s.handleUpdateStrategicResearchFrequency}
        onSyncTaskTypeDefaults={s.syncTaskTypeDefaults}
        healthCheckUrls={s.healthCheckUrls}
        onUpdateHealthCheckUrls={s.updateHealthCheckUrls}
        agentPromptSuffixes={s.agentPromptSuffixes}
        onUpdateAgentPromptSuffixes={s.updateAgentPromptSuffixes}
        githubAccount={s.githubAccount}
        onConnectGitHub={s.connectGitHub}
        maxPlanAccounts={s.maxPlanAccounts}
        onToggleAccount={s.toggleMaxPlanAccount}
        onRemoveAccount={s.removeMaxPlanAccount}
        onResetRateLimits={s.resetAccountRateLimits}
        newAccountName={s.newAccountName}
        setNewAccountName={s.setNewAccountName}
        newAccountToken={s.newAccountToken}
        setNewAccountToken={s.setNewAccountToken}
        onAddAccount={s.addMaxPlanAccount}
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
        userRole={s.userRole}
        showDiagnostics={s.showDiagnostics}
        onToggleDiagnostics={() => s.setShowDiagnostics(prev => !prev)}
        stuckTaskCount={s.diagnostics?.stuck_tasks.length ?? 0}
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
          />
        </ErrorBoundary>
      </div>


      {versionMismatch && (
        <div className="mx-4 mb-2 p-3 bg-yellow-900/50 border border-yellow-700 rounded-lg text-yellow-200 text-sm flex items-center gap-2">
          <AlertCircle size={16} />
          <span>Backend version mismatch — some features may not work as expected. Try refreshing the page.</span>
          <button onClick={() => setVersionMismatch(false)} className="ml-auto text-yellow-400 hover:text-yellow-200">&times;</button>
        </div>
      )}

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
        onViewTask={(taskId) => {
          const task = s.tasks.find(t => t.id === taskId);
          if (task) s.handleViewLogs(task);
          else fetchTask(taskId).then(s.handleViewLogs).catch(() => {});
        }}
      />

      {!s.showArchive && !s.showActivityFeed && !s.showUserManagement && (
        <CatchUpPanel
          selectedProjectId={s.selectedProjectId}
          lastVisitTimestamp={s.lastVisitTimestamp}
          refreshInterval={s.refreshInterval}
          needsAttentionCount={s.needsAttentionCount}
          onSelectTask={(task) => {
            fetchTask(task.id).then(s.handleViewLogs).catch(() => {
              s.addToast('Could not load task — it may have been deleted', 'error');
            });
          }}
          onOpenActivityFeed={(filter) => { setActivityFeedFilter(filter); s.setShowActivityFeed(true); }}
          onApprove={(taskId) => s.handleApprove(taskId)}
          onRetry={(taskId) => s.handleRetry(taskId)}
          onBulkApprove={(taskIds) => s.handleBulkAction('approve', taskIds)}
          onBulkRetry={(taskIds) => s.handleBulkAction('retry', taskIds)}
          wsRef={s.wsRef}
        />
      )}

      <OrchestratorHealthBar
        health={s.orchestratorHealth}
        detailedHealth={s.detailedHealth}
        apiHealthy={s.apiHealthy}
        wsConnected={s.wsConnected}
        lastFetchTime={s.lastFetchTime}
        tasks={s.tasks}
        pipelineThrottle={s.pipelineThrottle}
      />

      <div
        className="overflow-hidden transition-all duration-300 ease-in-out shrink-0"
        style={{ maxHeight: s.showDiagnostics ? '500px' : '0px', opacity: s.showDiagnostics ? 1 : 0 }}
      >
        <DiagnosticsPanel
          diagnostics={s.diagnostics}
          onViewTask={(taskId) => {
            fetchTask(taskId).then(s.handleViewLogs).catch(() => {
              s.addToast('Could not load task — it may have been deleted', 'error');
            });
          }}
        />
      </div>

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

      <div className="flex items-start gap-3">
        <div className="flex-1">
          <ProjectDashboard
            projects={s.projects}
            tasks={s.tasks}
            selectedProjectId={s.selectedProjectId}
            onSelectProject={s.setSelectedProjectId}
          />
        </div>
        {s.selectedProjectId && (
          <button
            onClick={() => setShowProjectMembers(prev => !prev)}
            className={`mt-1 p-2 rounded text-xs transition-colors border shrink-0 ${
              showProjectMembers
                ? 'bg-cyan-500/10 border-cyan-500/30 text-cyan-400'
                : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border-neutral-700'
            }`}
            title="Project Members"
          >
            <UserPlus className="w-4 h-4" />
          </button>
        )}
      </div>

      {showProjectMembers && s.selectedProjectId && selectedProject && (
        <ProjectMembers
          projectId={s.selectedProjectId}
          members={s.projectMembers}
          myRole={selectedProject.my_role || 'viewer'}
          onAdd={(email, role) => s.addProjectMember(s.selectedProjectId!, email, role)}
          onUpdateRole={(memberId, role) => s.updateProjectMemberRole(s.selectedProjectId!, memberId, role)}
          onRemove={(memberId) => s.removeProjectMember(s.selectedProjectId!, memberId)}
        />
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
      ) : s.showActivityFeed ? (
        <ActivityFeed
          selectedProjectId={s.selectedProjectId}
          refreshInterval={s.refreshInterval}
          onSelectTask={(task) => {
            s.setShowActivityFeed(false);
            fetchTask(task.id).then(s.handleViewLogs).catch(() => {
              s.addToast('Could not load task — it may have been deleted', 'error');
            });
          }}
          onClose={() => { s.setShowActivityFeed(false); setActivityFeedFilter(undefined); }}
          lastVisitTimestamp={s.prevLastActivityFeedOpen}
          onEventsChange={setActivityEvents}
          initialFilter={activityFeedFilter}
        />
      ) : s.showMessages ? (
        <MessagesPanel
          selectedProjectId={s.selectedProjectId}
          onClose={() => s.setShowMessages(false)}
          addToast={s.addToast}
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
          <div className="flex items-center gap-2 shrink-0">
            <SearchFilterBar
              searchQuery={s.searchQuery}
              onSearchChange={s.setSearchQuery}
              taskTypeFilter={s.taskTypeFilter}
              onTypeFilterChange={s.setTaskTypeFilter}
              statusFilter={s.statusFilter}
              onStatusFilterChange={s.setStatusFilter}
              filteredCount={filteredTasks.length}
              totalCount={s.tasks.length}
              inputRef={searchInputRef}
            />
          </div>

          <div className="flex items-center gap-2 shrink-0 mb-2">
            {s.viewMode === 'kanban' && (
            <div className="relative group">
              <button
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border bg-neutral-900/50 border-neutral-800/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300 transition-colors"
              >
                <Layers className="w-3.5 h-3.5" />
                Group: {s.groupBy === 'status' ? 'Status' : s.groupBy === 'type' ? 'Type' : 'Project'}
              </button>
              <div className="absolute left-0 top-full mt-1 z-20 hidden group-focus-within:flex group-hover:flex flex-col bg-neutral-900 border border-neutral-700 rounded-lg shadow-xl overflow-hidden min-w-[120px]">
                {(['status', 'type', 'project'] as const).map(opt => (
                  <button
                    key={opt}
                    onClick={() => s.setGroupBy(opt)}
                    className={`px-3 py-2 text-xs text-left hover:bg-neutral-800 transition-colors ${s.groupBy === opt ? 'text-amber-400 font-medium' : 'text-neutral-300'}`}
                  >
                    {opt === 'status' ? 'Status' : opt === 'type' ? 'Type' : 'Project'}
                  </button>
                ))}
              </div>
            </div>
            )}
            {s.viewMode === 'kanban' && (
            <button
              onClick={s.toggleSelectionMode}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border transition-colors ${
                s.selectionMode
                  ? 'bg-amber-500/20 border-amber-500/40 text-amber-400'
                  : 'bg-neutral-900/50 border-neutral-800/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300'
              }`}
            >
              <CheckSquare className="w-3.5 h-3.5" />
              {s.selectionMode ? `Select (${s.selectedTaskIds.size} selected)` : 'Select'}
            </button>
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
              <select
                value={sortBy}
                onChange={e => setSortBy(e.target.value as SortBy)}
                className="px-2 py-1.5 rounded-lg text-xs font-medium border bg-neutral-900/50 border-neutral-800/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300 transition-colors cursor-pointer"
                title="Sort tasks"
              >
                <option value="newest">Newest first</option>
                <option value="oldest">Oldest first</option>
                <option value="type">Type</option>
                <option value="title">Title</option>
              </select>
            )}
            {s.viewMode === 'kanban' && (
              <ColumnsPicker prefs={effectiveColumnPrefs} onPrefsChange={s.setColumnPrefs} />
            )}
            {s.selectionMode && s.selectedTaskIds.size > 0 && (
              <button
                onClick={() => s.setConfirmDialog({
                  title: `Archive ${s.selectedTaskIds.size} selected task${s.selectedTaskIds.size !== 1 ? 's' : ''}?`,
                  message: 'This will archive the selected tasks. They can be viewed and unarchived from the Archive view.',
                  confirmLabel: 'Archive Selected',
                  onConfirm: s.handleBulkArchiveSelected,
                })}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border bg-amber-500/20 border-amber-500/40 text-amber-400 hover:bg-amber-500/30 transition-colors"
              >
                <Archive className="w-3.5 h-3.5" />
                Archive Selected
              </button>
            )}
            <div className="flex-1" />
            <button
              onClick={() => {
                s.setConfirmDialog({
                  title: 'Archive all completed tasks?',
                  message: 'This will archive all completed tasks across all pages. They can be viewed and unarchived from the Archive view.',
                  confirmLabel: 'Archive All Completed',
                  onConfirm: () => s.handleBulkArchive('completed'),
                });
              }}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border bg-neutral-900/50 border-neutral-800/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300 transition-colors"
            >
              <Archive className="w-3.5 h-3.5" />
              Archive All Completed
            </button>
            <button
              onClick={() => {
                s.setConfirmDialog({
                  title: 'Archive all terminal tasks?',
                  message: 'This will archive all terminal tasks (completed, cancelled, and failed) across all pages. They can be viewed and unarchived from the Archive view.',
                  confirmLabel: 'Archive All Terminal',
                  onConfirm: () => s.handleBulkArchive('terminal'),
                });
              }}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border bg-neutral-900/50 border-neutral-800/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300 transition-colors"
            >
              <Archive className="w-3.5 h-3.5" />
              Archive All Terminal
            </button>
          </div>

          {s.viewMode === 'kanban' ? (
            <ErrorBoundary level="panel">
            <KanbanBoard
              tasks={filteredTasks}
              columns={visibleColumns}
              loading={s.initialLoading}
              maxRetries={s.maxRetries}
              actionLoadingMap={s.actionLoadingMap}
              selectionMode={s.selectionMode}
              selectedTaskIds={s.selectedTaskIds}
              compact={s.compactMode}
              sortBy={sortBy}
              onToggleTaskSelection={s.toggleTaskSelection}
              groupBy={s.groupBy}
              projects={s.projects}
              hasActiveFilters={hasActiveFilters}
              onBulkToggle={s.bulkColumnToggle}
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
            />
            </ErrorBoundary>
          ) : s.viewMode === 'timeline' ? (
            <ErrorBoundary level="panel">
            <main className="flex-1 overflow-hidden flex flex-col">
              <TimelineView
                tasks={filteredTasks}
                hasActiveFilters={hasActiveFilters}
                onViewLogs={s.handleViewLogs}
              />
            </main>
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
                onClearWaveDeps={s.selectedProjectId ? () => s.handleClearWaveDeps(s.selectedProjectId!) : undefined}
              />
            </main>
            </ErrorBoundary>
          )}

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

      {s.showResearchBrowser && (
        <ResearchBrowser
          tasks={s.tasks}
          onClose={() => s.setShowResearchBrowser(false)}
          onLaunch={(finding) => {
            s.setShowResearchBrowser(false);
            s.setNewTaskPrompt(finding.prompt);
            s.setNewTaskTitle(finding.title);
            s.setNewTaskType(finding.scope === 'large' ? 'plan' : 'create');
            s.openNewTaskModal();
          }}
        />
      )}

      {s.logTask && (
        <ErrorBoundary level="panel">
        <TaskDetailDrawer
          task={s.logTask}
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

      {s.showDirections && <DirectionsPanel onClose={() => s.setShowDirections(false)} addToast={s.addToast} />}
      <ToastContainer toasts={s.toasts} onDismiss={s.dismissToast} />

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
