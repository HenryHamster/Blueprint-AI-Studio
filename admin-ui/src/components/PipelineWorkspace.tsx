import type { ComponentProps, ReactNode } from 'react';
import ArchiveView from './ArchiveView';
import { ErrorBoundary } from './ErrorBoundary';
import { KanbanBoard } from './KanbanBoard';
import type { KanbanColumn, SortBy } from './KanbanBoard';
import { PipelineView } from './PipelineView';
import { TaskContextRail } from './TaskContextRail';
import type { Task } from './TaskCard';
import { WorkbenchMetric, WorkbenchPanel, WorkbenchSplit, WorkbenchSurface } from './WorkbenchLayout';
import type { PipelineSection } from '../hooks/useWorkbenchCommands';

type ArchiveProps = ComponentProps<typeof ArchiveView>;
type TaskContextRailProps = ComponentProps<typeof TaskContextRail>;

interface PipelineSurfaceProps {
  pipelineSection: PipelineSection;
  onPipelineSectionChange: (section: PipelineSection) => void;
  showArchive: boolean;
  setShowArchive: (show: boolean) => void;
  viewMode: 'pipeline' | 'kanban';
  setViewMode: (viewMode: 'pipeline' | 'kanban') => void;
}

interface PipelineChromeProps {
  actions: ReactNode;
  filterStrip: ReactNode;
  pagination: ReactNode;
}

interface PipelineMetricsProps {
  activeTasks: Task[];
  queuedTasks: Task[];
  blockedTasks: Task[];
  failedTasks: Task[];
  terminalTasks: Task[];
}

type PipelineArchiveProps = Omit<ArchiveProps, 'setShowArchive'>;

interface PipelineFlowProps {
  tasks: Task[];
  hasActiveFilters: boolean;
  onViewLogs: (task: Task) => void;
  onNavigateToParent: (parentTaskId: string) => void;
  onClearWaveDeps?: () => void;
  onArchiveGroup: (taskIds: string[]) => void;
  onUnarchiveTask: (taskId: string) => void;
}

interface PipelineRailProps {
  currentTask: Task | null;
  attentionTasks: Task[];
  localFallbackTasks: Task[];
  trackedTaskIds: Set<string>;
  taskContextSessionProps: Pick<TaskContextRailProps, 'sessionRefs' | 'savingSessionRefs' | 'onUpdateSessionRefs'>;
  onOpenTask: (task: Task) => void;
  onOpenPipeline: () => void;
  onTrackTask: (task: Task) => void;
}

interface PipelineBoardProps {
  tasks: Task[];
  columns: KanbanColumn[];
  loading: boolean;
  maxRetries: number;
  actionLoadingMap: Record<string, string>;
  compact: boolean;
  sortBy: SortBy;
  selectionMode: boolean;
  selectedTaskIds: Set<string>;
  onToggleTaskSelection: (id: string) => void;
  onCtrlClick: (taskId: string) => void;
  onBulkToggle: (taskIds: string[], selected: boolean) => void;
  onShiftSelect: (taskId: string, orderedTaskIds: string[]) => void;
  onArchiveColumn: (taskIds: string[], columnLabel: string) => void;
  onApprove: (id: string) => void;
  onRequestChanges: (id: string, feedback: string, action: 'fix' | 'review') => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onEditPrompt: (id: string, newPrompt: string) => void;
  onDismiss: (id: string) => void;
  onArchive: (id: string) => void;
  onViewLogs: (task: Task) => void;
  onTrack: (task: Task) => void;
  trackedTaskIds: Set<string>;
  onDuplicate: (task: Task) => void;
  hasActiveFilters: boolean;
  searchQuery: string;
  onDirectionClick: (directionId: string) => void;
}

export interface PipelineWorkspaceProps {
  surface: PipelineSurfaceProps;
  chrome: PipelineChromeProps;
  archive: PipelineArchiveProps;
  flow: PipelineFlowProps;
  rail: PipelineRailProps;
  board: PipelineBoardProps;
  metrics: PipelineMetricsProps;
}

export function PipelineWorkspace({
  surface,
  chrome,
  archive,
  flow,
  rail,
  board,
  metrics,
}: PipelineWorkspaceProps) {
  return (
    <WorkbenchSurface
      title="Pipeline"
      tabs={[
        { id: 'flow', label: 'Flow' },
        { id: 'board', label: 'Board' },
        { id: 'archive', label: 'Archive' },
      ]}
      activeTab={surface.pipelineSection}
      onTabChange={(id) => {
        surface.onPipelineSectionChange(id);
        surface.setShowArchive(id === 'archive');
        if (id === 'flow') surface.setViewMode('pipeline');
        if (id === 'board') surface.setViewMode('kanban');
      }}
      actions={chrome.actions}
      tabLayout="stacked"
      flush
    >
      {surface.pipelineSection === 'archive' && surface.showArchive ? (
        <ArchiveView
          {...archive}
          setShowArchive={(show) => {
            surface.setShowArchive(show);
            if (!show) {
              surface.onPipelineSectionChange('flow');
              surface.setViewMode('pipeline');
            }
          }}
        />
      ) : surface.viewMode === 'pipeline' ? (
        <WorkbenchSplit
          main={(
            <div className="flex h-full min-h-0 flex-col">
              {chrome.filterStrip}
              <ErrorBoundary level="panel">
                <main className="flex min-h-0 flex-1 flex-col overflow-hidden px-3 py-3 md:px-4">
                  <PipelineView
                    tasks={flow.tasks}
                    hasActiveFilters={flow.hasActiveFilters}
                    onViewLogs={flow.onViewLogs}
                    onNavigateToParent={flow.onNavigateToParent}
                    onClearWaveDeps={flow.onClearWaveDeps}
                    onArchiveGroup={flow.onArchiveGroup}
                    onUnarchiveTask={flow.onUnarchiveTask}
                  />
                </main>
              </ErrorBoundary>
              {chrome.pagination}
            </div>
          )}
          rail={(
            <div className="space-y-3">
              <WorkbenchPanel title="Pipeline" meta={flow.hasActiveFilters ? 'filtered' : 'live'}>
                <div className="grid grid-cols-2 gap-2">
                  <WorkbenchMetric label="active" value={metrics.activeTasks.length} tone="good" />
                  <WorkbenchMetric label="queued" value={metrics.queuedTasks.length} tone="neutral" />
                  <WorkbenchMetric label="blocked" value={metrics.blockedTasks.length} tone={metrics.blockedTasks.length > 0 ? 'warn' : 'neutral'} />
                  <WorkbenchMetric label="failed" value={metrics.failedTasks.length} tone={metrics.failedTasks.length > 0 ? 'bad' : 'neutral'} />
                </div>
                <div className="mt-3 flex items-center justify-between rounded-md border border-neutral-800 bg-neutral-900/40 px-3 py-2 text-xs">
                  <span className="text-neutral-500">terminal</span>
                  <span className="font-medium text-neutral-200">{metrics.terminalTasks.length}</span>
                </div>
              </WorkbenchPanel>

              <TaskContextRail
                currentTask={rail.currentTask}
                attentionTasks={rail.attentionTasks}
                localFallbackTasks={rail.localFallbackTasks}
                trackedTaskIds={rail.trackedTaskIds}
                {...rail.taskContextSessionProps}
                onOpenTask={rail.onOpenTask}
                onOpenPipeline={rail.onOpenPipeline}
                onTrackTask={rail.onTrackTask}
              />
            </div>
          )}
        />
      ) : (
        <div className="flex h-full min-h-0 flex-col">
          {chrome.filterStrip}
          <ErrorBoundary level="panel">
            <div className="min-h-0 flex-1 overflow-hidden px-3 py-3 md:px-4">
              <KanbanBoard
                tasks={board.tasks}
                columns={board.columns}
                loading={board.loading}
                maxRetries={board.maxRetries}
                actionLoadingMap={board.actionLoadingMap}
                compact={board.compact}
                sortBy={board.sortBy}
                selectionMode={board.selectionMode}
                selectedTaskIds={board.selectedTaskIds}
                onToggleTaskSelection={board.onToggleTaskSelection}
                onCtrlClick={board.onCtrlClick}
                onBulkToggle={board.onBulkToggle}
                onShiftSelect={board.onShiftSelect}
                hasActiveFilters={board.hasActiveFilters}
                onArchiveColumn={board.onArchiveColumn}
                onApprove={board.onApprove}
                onRequestChanges={board.onRequestChanges}
                onCancel={board.onCancel}
                onRetry={board.onRetry}
                onEditPrompt={board.onEditPrompt}
                onDismiss={board.onDismiss}
                onArchive={board.onArchive}
                onViewLogs={board.onViewLogs}
                onTrack={board.onTrack}
                trackedTaskIds={board.trackedTaskIds}
                onDuplicate={board.onDuplicate}
                searchQuery={board.searchQuery}
                onDirectionClick={board.onDirectionClick}
              />
            </div>
          </ErrorBoundary>
          {chrome.pagination}
        </div>
      )}
    </WorkbenchSurface>
  );
}
