import { useEffect, useState, type ComponentProps, type ReactNode } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import ArchiveView from './ArchiveView';
import { ErrorBoundary } from './ErrorBoundary';
import { KanbanBoard } from './KanbanBoard';
import type { KanbanColumn, SortBy } from './KanbanBoard';
import { PipelineView } from './PipelineView';
import { TaskContextRail } from './TaskContextRail';
import TaskStreamPanel, { type TaskStreamPanelProps } from './TaskStreamPanel';
import type { Task } from './TaskCard';
import { WorkbenchSurface } from './WorkbenchLayout';
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

interface PipelineSideboardProps {
  flow: PipelineFlowProps;
  rail: PipelineRailProps;
  metrics: PipelineMetricsProps;
  stream?: PipelineStreamProps;
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

function formatRowsLabel(status: PipelineWorkspaceStatus): string {
  if (status.displayedRows === status.loadedRows) return `${status.displayedRows} shown`;
  return `${status.displayedRows}/${status.loadedRows} shown`;
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
    neutral: 'border-neutral-800/80 bg-neutral-950/35 text-neutral-400',
    good: 'border-emerald-500/20 bg-emerald-500/10 text-emerald-300',
    warn: 'border-amber-500/20 bg-amber-500/10 text-amber-300',
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

function PipelineStatusDetail({
  label,
  value,
  tone = 'neutral',
  title,
}: {
  label: string;
  value: ReactNode;
  tone?: 'neutral' | 'good' | 'warn' | 'bad' | 'info';
  title?: string;
}) {
  const valueClass = {
    neutral: 'text-neutral-200',
    good: 'text-emerald-300',
    warn: 'text-amber-300',
    bad: 'text-red-300',
    info: 'text-cyan-300',
  }[tone];

  return (
    <div className="min-w-0 bg-[#070b11] px-3 py-2" title={title}>
      <dt className="text-ui-sm font-semibold uppercase text-neutral-600">{label}</dt>
      <dd className={`mt-0.5 min-w-0 truncate font-mono text-ui-md ${valueClass}`}>{value}</dd>
    </div>
  );
}

function PipelineStatusStrip({ status }: { status: PipelineWorkspaceStatus }) {
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [detailsOpen, setDetailsOpen] = useState(false);
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
    ? `${scopeValue}`
    : 'All projects';
  const rowLabel = formatRowsLabel(status);
  const streamLabel = typeof status.wsConnected === 'boolean'
    ? (status.wsConnected ? 'Stream on' : 'Stream off')
    : null;

  return (
    <section className="border-b border-neutral-800 bg-[#070b11] text-xs" data-testid="pipeline-status-strip">
      <div
        data-viewport-allow-overflow
        className="flex w-full min-w-0 max-w-full items-center gap-1.5 px-2.5 py-1.5"
      >
        <button
          type="button"
          aria-expanded={detailsOpen}
          aria-controls="pipeline-status-details"
          onClick={() => setDetailsOpen(open => !open)}
          className="inline-flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded border border-neutral-800 bg-neutral-950/45 text-neutral-500 transition-colors hover:border-neutral-700 hover:text-neutral-200 sm:min-h-7 sm:min-w-7"
          title={detailsOpen ? 'Hide monitoring details' : 'Show monitoring details'}
        >
          {detailsOpen ? <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" /> : <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />}
          <span className="sr-only">{detailsOpen ? 'Hide monitoring details' : 'Show monitoring details'}</span>
        </button>
        <div className="flex min-w-0 flex-1 items-center gap-1.5 overflow-hidden">
          <span className="hidden shrink-0 text-ui-sm font-semibold uppercase text-neutral-600 sm:inline">Task monitor</span>
          <PipelineStatusPill
            label={scopeLabel}
            tone={scoped ? 'info' : 'neutral'}
            title={scoped ? `Project-scoped task view: ${scopeValue}` : 'Global task view'}
            className="shrink min-w-[4rem] max-w-40 md:max-w-64"
          />
          <PipelineStatusPill
            label={rowLabel}
            tone={status.displayedRows !== status.loadedRows ? 'info' : 'neutral'}
            title="Rows currently displayed compared with the loaded page"
            className="hidden sm:inline-flex"
          />
          <span
            className={`min-w-0 truncate font-mono text-ui-md ${stale ? 'text-amber-300' : 'text-neutral-400'}`}
            title="Task data freshness from the existing poller state"
          >
            {formatFetchLabel(status.lastFetchTime, stale)}
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <PipelineStatusPill label={apiLabel} tone={apiTone} title="Task API health from the existing poller state" />
          {streamLabel && (
            <PipelineStatusPill
              label={streamLabel}
              tone={status.wsConnected ? 'good' : 'bad'}
              title="Websocket state from the existing stream"
              className="hidden sm:inline-flex"
            />
          )}
        </div>
      </div>
      {detailsOpen && (
        <dl
          id="pipeline-status-details"
          data-testid="pipeline-status-details"
          className="grid min-w-0 grid-cols-2 gap-px border-t border-neutral-800/80 bg-neutral-900/70 sm:grid-cols-3 lg:grid-cols-6"
        >
          <PipelineStatusDetail
            label="Scope"
            value={scopeLabel}
            tone={scoped ? 'info' : 'neutral'}
            title={scoped ? `Project-scoped task view: ${scopeValue}` : 'Global task view'}
          />
          <PipelineStatusDetail
            label="Displayed"
            value={status.displayedRows}
            tone={status.displayedRows !== status.loadedRows ? 'info' : 'neutral'}
            title="Rows currently displayed after filters"
          />
          <PipelineStatusDetail
            label="Loaded"
            value={status.loadedRows}
            title="Rows loaded in the current server page"
          />
          <PipelineStatusDetail
            label="Server total"
            value={status.serverTotalRows}
            title="Total rows reported by the server for this scope"
          />
          <PipelineStatusDetail
            label="API"
            value={status.apiHealthy ? 'healthy' : 'degraded'}
            tone={apiTone}
            title="Task API health from the existing poller state"
          />
          <PipelineStatusDetail
            label="Event stream"
            value={typeof status.wsConnected === 'boolean' ? (status.wsConnected ? 'connected' : 'disconnected') : 'not reported'}
            tone={typeof status.wsConnected === 'boolean' ? (status.wsConnected ? 'good' : 'bad') : 'neutral'}
            title="Websocket state from the existing stream"
          />
        </dl>
      )}
    </section>
  );
}

function PipelineHealthRow({
  label,
  value,
  tone = 'neutral',
}: {
  label: string;
  value: number;
  tone?: 'neutral' | 'good' | 'warn' | 'bad' | 'info';
}) {
  const toneClass = {
    neutral: { dot: 'bg-neutral-500', value: 'text-neutral-200' },
    good: { dot: 'bg-emerald-400', value: 'text-emerald-200' },
    warn: { dot: 'bg-amber-400', value: 'text-amber-200' },
    bad: { dot: 'bg-red-400', value: 'text-red-200' },
    info: { dot: 'bg-cyan-400', value: 'text-cyan-200' },
  }[tone];

  return (
    <div className="flex min-w-0 items-center justify-between gap-3 px-2.5 py-1.5">
      <dt className="flex min-w-0 items-center gap-2 text-xs text-neutral-400">
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${toneClass.dot}`} aria-hidden="true" />
        <span className="truncate">{label}</span>
      </dt>
      <dd className={`shrink-0 font-mono text-sm font-semibold ${toneClass.value}`}>{value}</dd>
    </div>
  );
}

function PipelineSideboard({ flow, rail, metrics, stream }: PipelineSideboardProps) {
  const attentionCount = metrics.blockedTasks.length + metrics.failedTasks.length;
  const attentionLabel = attentionCount > 0
    ? `${attentionCount} need attention`
    : 'clear';

  return (
    <div data-testid="pipeline-sideboard" className="grid min-w-0 gap-1.5">
      <section
        data-testid="pipeline-health-panel"
        className="min-w-0 overflow-hidden rounded border border-neutral-800/80 bg-[#0b1118]/90"
      >
        <div className="flex min-w-0 items-center justify-between gap-3 border-b border-neutral-800/80 px-2.5 py-2">
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold text-neutral-100">Task health</h2>
            <div className="mt-0.5 truncate text-ui-sm text-neutral-600">{flow.hasActiveFilters ? 'Filtered task monitor' : 'Live task monitor'}</div>
          </div>
          <span className={`shrink-0 rounded border px-2 py-0.5 font-mono text-ui-sm ${attentionCount > 0 ? 'border-amber-500/20 bg-amber-500/10 text-amber-300' : 'border-neutral-800 bg-neutral-950/40 text-neutral-500'}`}>
            {attentionLabel}
          </span>
        </div>
        <dl className="divide-y divide-neutral-800/70">
          <PipelineHealthRow label="Running" value={metrics.activeTasks.length} tone="good" />
          <PipelineHealthRow label="Queued" value={metrics.queuedTasks.length} />
          <PipelineHealthRow label="Blocked" value={metrics.blockedTasks.length} tone={metrics.blockedTasks.length > 0 ? 'warn' : 'neutral'} />
          <PipelineHealthRow label="Failed" value={metrics.failedTasks.length} tone={metrics.failedTasks.length > 0 ? 'bad' : 'neutral'} />
          <PipelineHealthRow label="Finished" value={metrics.terminalTasks.length} tone="info" />
        </dl>
      </section>

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
      title="Pipeline tasks"
      tabs={[
        { id: 'flow', label: 'Tasks' },
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
      {surface.pipelineSection === 'archive' ? (
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
      ) : surface.pipelineSection === 'flow' ? (
        <div className="flex h-full min-h-0 flex-col">
          {chrome.filterStrip}
          {status && <PipelineStatusStrip status={status} />}
          <div className="grid min-h-0 min-w-0 flex-1 grid-cols-1 grid-rows-[auto_minmax(0,1fr)] overflow-hidden lg:grid-cols-[minmax(0,1fr)_minmax(300px,25vw)] lg:grid-rows-1">
            <aside className="max-h-[34vh] min-h-0 min-w-0 overflow-auto border-b border-neutral-800 bg-[#0b1118] p-1.5 custom-scrollbar lg:col-start-2 lg:row-start-1 lg:max-h-none lg:border-b-0 lg:border-l">
              <PipelineSideboard
                flow={flow}
                rail={rail}
                metrics={metrics}
                stream={stream}
              />
            </aside>
            <ErrorBoundary level="panel">
              <main data-testid="pipeline-flow-main" className="flex min-h-0 min-w-0 flex-col overflow-hidden px-3 py-2.5 md:px-4 lg:col-start-1 lg:row-start-1 lg:py-3">
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
          </div>
          {chrome.pagination}
        </div>
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
