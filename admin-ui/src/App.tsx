import { useMemo, useState } from 'react';
import { Activity, AlertCircle } from 'lucide-react';
import { useAppState } from './useAppState';
import { SummaryPanels } from './components/SummaryPanels';
import { DirectionsPanel } from './components/DirectionsPanel';
import { ResearchBrowser } from './components/ResearchBrowser';
import { PipelineView } from './components/PipelineView';
import { ProjectDashboard } from './components/ProjectDashboard';
import { auth, signInWithGoogle, signOut } from './firebase';
import { TaskDetailDrawer } from './components/TaskDetailDrawer';
import { ToastContainer } from './components/Toasts';
import { NewTaskModal } from './components/NewTaskModal';
import { HeaderBar } from './components/HeaderBar';
import { StatusBar } from './components/StatusBar';
import MetricsDashboard from './components/MetricsDashboard';
import { SearchFilterBar } from './components/SearchFilterBar';
import { KanbanBoard } from './components/KanbanBoard';
import type { KanbanColumn } from './components/KanbanBoard';
import { Pagination } from './components/Pagination';
import ArchiveView from './components/ArchiveView';

const ACTIVE_STATUSES = new Set(['QUEUED', 'PROVISIONING', 'IN_PROGRESS', 'EVALUATING', 'FAILED']);

const COLUMNS: KanbanColumn[] = [
  { id: 'QUEUED', label: 'Queued', includes: ['QUEUED', 'PROVISIONING'] },
  { id: 'IN_PROGRESS', label: 'In Progress', includes: ['IN_PROGRESS'] },
  { id: 'EVALUATING', label: 'Evaluating', includes: ['EVALUATING'] },
  { id: 'FAILED', label: 'Failed', includes: ['FAILED'] },
  { id: 'CANCELLED', label: 'Cancelled', includes: ['CANCELLED'] },
  { id: 'COMPLETED', label: 'Completed', includes: ['COMPLETED'] },
];

export default function App() {
  const s = useAppState();
  const [hideEmptyColumns, setHideEmptyColumns] = useState(true);

  const actionRequiredTasks = useMemo(() => s.tasks.filter(t => t.requires_approval || t.status === 'FAILED'), [s.tasks]);
  const runningTasks = useMemo(() => s.tasks.filter(t => t.status === 'IN_PROGRESS' || t.status === 'PROVISIONING'), [s.tasks]);
  const queuedTasks = useMemo(() => s.tasks.filter(t => t.status === 'QUEUED'), [s.tasks]);
  const completedRecently = useMemo(() => {
    const oneDayAgo = new Date(new Date().getTime() - 24 * 60 * 60 * 1000);
    return s.tasks.filter(t => {
      if (t.status !== 'COMPLETED' && t.status !== 'CANCELLED') return false;
      const completedAt = t.history && t.history.length > 0
        ? t.history[t.history.length - 1].timestamp
        : t.created_at;
      return new Date(completedAt) > oneDayAgo;
    });
  }, [s.tasks]);

  const filteredTasks = useMemo(() => s.tasks.filter(t => {
    const taskType = t.task_type || (t.payloads?.[0] as Record<string, unknown>)?.type as string || 'create';
    const typeMatch = s.taskTypeFilter === 'all' || taskType === s.taskTypeFilter;
    const query = s.searchQuery.toLowerCase();
    const searchMatch = !query || (t.title?.toLowerCase().includes(query)) || (t.description?.toLowerCase().includes(query));
    const activeMatch = !s.activeOnly || ACTIVE_STATUSES.has(t.status) || t.requires_approval === true;
    const projectMatch = s.selectedProjectId === null || t.project_id === s.selectedProjectId;
    return typeMatch && searchMatch && activeMatch && projectMatch;
  }), [s.tasks, s.taskTypeFilter, s.searchQuery, s.activeOnly, s.selectedProjectId]);

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
          <h1 className="text-lg font-semibold text-neutral-100 mb-2">Blueprint Admin</h1>
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

  return (
    <div className="h-screen overflow-hidden flex flex-col bg-neutral-950 text-neutral-200 font-sans p-6">
      <HeaderBar
        runningTasks={runningTasks}
        queuedTasks={queuedTasks}
        globalActiveCount={s.globalActiveCount}
        globalEvaluatingCount={s.globalEvaluatingCount}
        globalQueuedCount={s.globalQueuedCount}
        maxConcurrentVMs={s.maxConcurrentVMs}
        usingMaxPlan={s.usingMaxPlan}
        lastRefresh={s.lastRefresh}
        onRefresh={s.fetchTasks}
        viewMode={s.viewMode}
        defaultTaskType={s.defaultTaskType}
        onUpdateDefaultTaskType={s.setDefaultTaskType}
        notifyOnComplete={s.notifyOnComplete}
        onUpdateNotifyOnComplete={s.setNotifyOnComplete}
        onToggleViewMode={() => s.setViewMode(v => v === 'kanban' ? 'pipeline' : 'kanban')}
        showResearchBrowser={s.showResearchBrowser}
        onToggleResearchBrowser={() => s.setShowResearchBrowser(prev => !prev)}
        showArchive={s.showArchive}
        onToggleArchive={() => s.setShowArchive(!s.showArchive)}
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
        onUpdateMaxVMs={s.updateMaxVMs}
        onUpdateMaxRetries={s.updateMaxRetries}
        githubAccount={s.githubAccount}
        onConnectGitHub={s.connectGitHub}
        maxPlanAccounts={s.maxPlanAccounts}
        onToggleAccount={s.toggleMaxPlanAccount}
        onRemoveAccount={s.removeMaxPlanAccount}
        newAccountName={s.newAccountName}
        setNewAccountName={s.setNewAccountName}
        newAccountToken={s.newAccountToken}
        setNewAccountToken={s.setNewAccountToken}
        onAddAccount={s.addMaxPlanAccount}
        showMetrics={s.showMetrics}
        onToggleMetrics={() => s.setShowMetrics(prev => !prev)}
        refreshInterval={s.refreshInterval}
        onUpdateRefreshInterval={s.setRefreshInterval}
      />

      <StatusBar
        apiHealthy={s.apiHealthy}
        wsConnected={s.wsConnected}
        lastFetchTime={s.lastFetchTime}
      />

      <div
        className="overflow-hidden transition-all duration-300 ease-in-out shrink-0"
        style={{ maxHeight: s.showMetrics ? '500px' : '0px', opacity: s.showMetrics ? 1 : 0 }}
      >
        <MetricsDashboard
          metricsHistory={s.metricsHistory}
          cumulativeCost={s.cumulativeCost}
          maxConcurrentVMs={s.maxConcurrentVMs}
          tasks={s.tasks}
        />
      </div>

      <SummaryPanels actionRequiredTasks={actionRequiredTasks} runningTasks={runningTasks} completedRecently={completedRecently} queuedTasks={queuedTasks} onSelectTask={s.handleViewLogs} onApproveTask={s.handleApprove} />

      {s.error && (
        <div className="mb-6 bg-red-950/50 text-red-400 px-4 py-3 rounded-lg text-sm border border-red-900/50 flex items-center gap-2 shrink-0">
          <AlertCircle className="w-4 h-4" />
          <p>{s.error}</p>
        </div>
      )}

      <ProjectDashboard
        projects={s.projects}
        tasks={s.tasks}
        selectedProjectId={s.selectedProjectId}
        onSelectProject={s.setSelectedProjectId}
      />

      {s.showArchive ? (
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
              activeOnly={s.activeOnly}
              onToggleActiveOnly={() => s.setActiveOnly(v => !v)}
            />
            {s.viewMode === 'kanban' && (
              <button
                onClick={() => setHideEmptyColumns(v => !v)}
                className="text-[11px] px-2.5 py-1 rounded border border-neutral-700 bg-neutral-800 hover:bg-neutral-700 text-neutral-400 hover:text-neutral-200 transition-colors shrink-0"
              >
                {hideEmptyColumns ? 'Show empty' : 'Hide empty'}
              </button>
            )}
          </div>

          {s.viewMode === 'kanban' ? (
            <KanbanBoard
              tasks={filteredTasks}
              columns={COLUMNS}
              loading={s.loading}
              maxRetries={s.maxRetries}
              actionLoadingMap={s.actionLoadingMap}
              hideEmptyColumns={hideEmptyColumns}
              onApprove={s.handleApprove}
              onCancel={s.handleCancel}
              onRetry={s.handleRetry}
              onEditPrompt={s.handleEditPrompt}
              onDismiss={s.handleDismiss}
              onArchive={s.handleArchiveTask}
              onViewLogs={s.handleViewLogs}
            />
          ) : (
            <main className="flex-1 overflow-hidden flex flex-col">
              <PipelineView
                tasks={filteredTasks}
                onViewLogs={s.handleViewLogs}
                onNavigateToParent={(parentTaskId) => {
                  navigator.clipboard.writeText(parentTaskId).then(() => {
                    s.addToast('Parent task ID copied — use search to find it', 'info');
                  }).catch(() => {
                    s.addToast(`Could not copy to clipboard — parent task ID: ${parentTaskId}`, 'error');
                  });
                }}
              />
            </main>
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
        <TaskDetailDrawer
          task={s.logTask}
          logLines={s.logLines}
          logsConnecting={s.logsConnecting}
          wsDisconnected={s.wsDisconnected}
          actionLoading={s.actionLoadingMap[s.logTask.id]}
          onClose={s.closeLogViewer}
          onApprove={s.handleApprove}
          onCancel={s.handleCancel}
          onRetry={s.handleRetry}
          onEditPrompt={s.handleEditPrompt}
          onDismiss={s.handleDismiss}
        />
      )}

      {s.showDirections && <DirectionsPanel onClose={() => s.setShowDirections(false)} />}
      <ToastContainer toasts={s.toasts} onDismiss={s.dismissToast} />
    </div>
  );
}
