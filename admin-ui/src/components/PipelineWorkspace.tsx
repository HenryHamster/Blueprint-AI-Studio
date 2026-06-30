import { useEffect, useState, type ComponentProps, type ReactNode } from 'react';
import ArchiveView from './ArchiveView';
import { ErrorBoundary } from './ErrorBoundary';
import { KanbanBoard } from './KanbanBoard';
import type { KanbanColumn, SortBy } from './KanbanBoard';
import { PipelineView } from './PipelineView';
import { TaskContextRail } from './TaskContextRail';
import TaskStreamPanel, { type TaskStreamPanelProps } from './TaskStreamPanel';
import type { Task } from './TaskCard';
import { WorkbenchMetric, WorkbenchPanel, WorkbenchSplit, WorkbenchSurface } from './WorkbenchLayout';
import type { PipelineSection } from '../hooks/useWorkbenchCommands';
import { timeAgo } from '../utils/time';

type ArchiveProps = ComponentProps<typeof ArchiveView>;
type TaskContextRailProps = ComponentProps<typeof TaskContextRail>;
type PipelineStreamProps = Pick<TaskStreamPanelProps, 'trackedTaskIds' | 'trackedLogs' | 'tasks' | 'onUntrack' | 'onUntrackAll'>;

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

export interface PipelineWorkspaceStatus {
  selectedProjectId?: string | null;
  selectedProjectLabel?: string | null;
  displayedRows: number;
  loadedRows: number;
  serverTotalRows: number;
  lastFetchTime?: number | Date | null;
  apiHealthy: boolean;
  wsConnected?: boolean | null;
  staleAfterMs?: number;
}

export interface PipelineWorkspaceProps {
  surface: PipelineSurfaceProps;
  chrome: PipelineChromeProps;
  archive: PipelineArchiveProps;
  flow: PipelineFlowProps;
  rail: PipelineRailProps;
  board: PipelineBoardProps;
  metrics: PipelineMetricsProps;
  status?: PipelineWorkspaceStatus;
  stream?: PipelineStreamProps;
}

const DEFAULT_STALE_AFTER_MS = 2 * 60 * 1000;

function normalizeFetchMs(value: number | Date | null | undefined): number | null {
  if (value == null) return null;
  if (value instanceof Date) return value.getTime();
  return value > 0 ? value : null;
}

function formatFetchLabel(value: number | Date | null | undefined, stale: boolean): string {
  const ms = normalizeFetchMs(value);
  if (ms == null) return 'fetch pending';
  return stale ? `data stale ${timeAgo(ms)}` : `fetched ${timeAgo(ms)}`;
}

function PipelineStatusPill({
  label,
  tone = 'neutral',
  title,
  className = '',
}: {
  label: string;
  tone?: 'neutral' | 'good' | 'warn' | 'bad' | 'info';
  title?: string;
  className?: string;
}) {
  const toneClass = {
    neutral: 'border-neutral-800 bg-neutral-950/40 text-neutral-400',
    good: 'border-emerald-500/20 bg-emerald-500/10 text-emerald-300',
    warn: 'border-yellow-500/20 bg-yellow-500/10 text-yellow-300',
    bad: 'border-red-500/25 bg-red-500/10 text-red-300',
    info: 'border-cyan-500/20 bg-cyan-500/10 text-cyan-300',
  }[tone];
  return (
    <span
      className={`inline-flex min-h-6 max-w-full items-center rounded border px-2 font-mono text-ui-md whitespace-nowrap ${toneClass} ${className}`}
      title={title}
    >
      <span className="min-w-0 truncate" title={title}>{label}</span>
    </span>
  );
}

function PipelineStatusStrip({ status }: { status: PipelineWorkspaceStatus }) {
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNowMs(Date.now()), 30_000);
    return () => window.clearInterval(id);
  }, []);
  const fetchMs = normalizeFetchMs(status.lastFetchTime);
  const staleAfterMs = status.staleAfterMs ?? DEFAULT_STALE_AFTER_MS;
  const stale = fetchMs != null && nowMs - fetchMs > staleAfterMs;
  const apiLabel = status.apiHealthy ? 'API healthy' : 'API degraded';
  const apiTone = status.apiHealthy ? 'good' : 'bad';
  const scoped = Boolean(status.selectedProjectId);
  const scopeValue = status.selectedProjectLabel || status.selectedProjectId;
  const scopeLabel = scoped
    ? `Project: ${scopeValue}`
    : 'Global';

  return (
    <div className="flex min-w-0 flex-nowrap items-center gap-1.5 overflow-x-auto border-b border-neutral-800 bg-[#070b11] px-3 py-1.5 text-xs">
      <PipelineStatusPill
        label={scopeLabel}
        tone={scoped ? 'info' : 'neutral'}
        title={scoped ? `Project-scoped task view: ${scopeValue}` : 'Global task view'}
        className="shrink min-w-[4rem] max-w-52 md:max-w-72"
      />
      <PipelineStatusPill
        label={`displayed ${status.displayedRows}`}
        tone={status.displayedRows !== status.loadedRows ? 'info' : 'neutral'}
        title="Rows currently displayed after filters"
      />
      <PipelineStatusPill label={`loaded ${status.loadedRows}`} title="Rows loaded in the current server page" />
      <PipelineStatusPill label={`server ${status.serverTotalRows}`} title="Total rows reported by the server for this scope" />
      <PipelineStatusPill
        label={formatFetchLabel(status.lastFetchTime, stale)}
        tone={stale ? 'warn' : 'neutral'}
        title="Task data freshness from the existing poller state"
      />
      <PipelineStatusPill label={apiLabel} tone={apiTone} title="Task API health from the existing poller state" />
      {typeof status.wsConnected === 'boolean' && (
        <PipelineStatusPill
          label={status.wsConnected ? 'WS connected' : 'WS disconnected'}
          tone={status.wsConnected ? 'good' : 'bad'}
          title="Websocket state from the existing stream"
        />
      )}
    </div>
  );
}

export function PipelineWorkspace({
  surface,
  chrome,
  archive,
  flow,
  rail,
  board,
  metrics,
  status,
  stream,
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
              {status && <PipelineStatusStrip status={status} />}
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

              {stream && stream.trackedTaskIds.size > 0 && (
                <TaskStreamPanel
                  {...stream}
                  variant="embedded"
                />
              )}

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
          {status && <PipelineStatusStrip status={status} />}
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
