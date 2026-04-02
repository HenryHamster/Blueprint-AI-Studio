import { useMemo } from 'react';
import { Activity, AlertCircle, Archive, CheckSquare } from 'lucide-react';
import { ConfirmDialog } from './components/ConfirmDialog';
import { useAppState } from './useAppState';
import { SummaryPanels } from './components/SummaryPanels';
import { DirectionsPanel } from './components/DirectionsPanel';
import { ResearchBrowser } from './components/ResearchBrowser';
import { PipelineView } from './components/PipelineView';
import { ProjectDashboard } from './components/ProjectDashboard';
import { auth, signInWithGoogle, signOut } from './firebase';
import { fetchTask } from './api';
import { TaskDetailDrawer } from './components/TaskDetailDrawer';
import { ToastContainer } from './components/Toasts';
import { NewTaskModal } from './components/NewTaskModal';
import { HeaderBar } from './components/HeaderBar';
import MetricsDashboard from './components/MetricsDashboard';
import { SearchFilterBar } from './components/SearchFilterBar';
import { KanbanBoard } from './components/KanbanBoard';
import type { KanbanColumn } from './components/KanbanBoard';
import { Pagination } from './components/Pagination';
import ArchiveView from './components/ArchiveView';

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
    const typeMatch = s.taskTypeFilter.length === 0 || s.taskTypeFilter.includes(taskType);
    const statusMatch = s.statusFilter.length === 0 || s.statusFilter.includes(t.status);
    const query = s.searchQuery.toLowerCase();
    const searchMatch = !query || (t.title?.toLowerCase().includes(query)) || (t.description?.toLowerCase().includes(query));
    const projectMatch = s.selectedProjectId === null || t.project_id === s.selectedProjectId;
    return typeMatch && statusMatch && searchMatch && projectMatch;
  }), [s.tasks, s.taskTypeFilter, s.statusFilter, s.searchQuery, s.selectedProjectId]);

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
        tasks={s.tasks}
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
        soundOnComplete={s.soundOnComplete}
        onUpdateSoundOnComplete={s.setSoundOnComplete}
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
        maxAutonomousTasksPerHour={s.maxAutonomousTasksPerHour}
        onUpdateMaxVMs={s.updateMaxVMs}
        onUpdateMaxRetries={s.updateMaxRetries}
        onUpdateMaxAutonomousRate={s.handleUpdateMaxAutonomousRate}
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
        onRollback={s.handleRollback}
        onMarkStable={s.handleMarkStable}
        onRepairState={s.handleRepairState}
      />

      <div
        className="overflow-hidden transition-all duration-300 ease-in-out shrink-0"
        style={{ maxHeight: s.showMetrics ? '700px' : '0px', opacity: s.showMetrics ? 1 : 0 }}
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
            />
          </div>

          <div className="flex items-center gap-2 shrink-0 mb-2">
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
            <KanbanBoard
              tasks={filteredTasks}
              columns={COLUMNS}
              loading={s.loading}
              maxRetries={s.maxRetries}
              actionLoadingMap={s.actionLoadingMap}
              selectionMode={s.selectionMode}
              selectedTaskIds={s.selectedTaskIds}
              onToggleTaskSelection={s.toggleTaskSelection}
              onArchiveColumn={(taskIds, columnLabel) => {
                s.setConfirmDialog({
                  title: `Archive all ${columnLabel} tasks?`,
                  message: `This will archive ${taskIds.length} task${taskIds.length !== 1 ? 's' : ''} in the ${columnLabel} column. They can be viewed and unarchived from the Archive view.`,
                  confirmLabel: `Archive ${columnLabel}`,
                  onConfirm: () => s.handleBulkArchiveColumn(taskIds),
                });
              }}
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

      {s.showDirections && <DirectionsPanel onClose={() => s.setShowDirections(false)} />}
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
  );
}
