import { afterEach, describe, expect, test, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { PipelineWorkspace, type PipelineWorkspaceProps } from './PipelineWorkspace';
import type { Task } from './TaskCard';
import type { TrackLogEntry } from '../types/task';

let capturedKanbanProps: Record<string, unknown> | null = null;

vi.mock('./ArchiveView', () => ({
  default: ({ setShowArchive }: { setShowArchive: (show: boolean) => void }) => (
    <div data-testid="archive-view">
      <button type="button" onClick={() => setShowArchive(false)}>Close archive mock</button>
    </div>
  ),
}));

vi.mock('./PipelineView', () => ({
  PipelineView: () => <div data-testid="pipeline-view" />,
}));

vi.mock('./KanbanBoard', () => ({
  KanbanBoard: (props: Record<string, unknown>) => {
    capturedKanbanProps = props;
    return <div data-testid="kanban-board" />;
  },
}));

vi.mock('./TaskContextRail', () => ({
  TaskContextRail: () => <div data-testid="task-context-rail" />,
}));

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-x',
    title: 'Task X',
    description: '',
    status: 'QUEUED',
    task_type: 'create',
    created_by: 'USER',
    requires_approval: false,
    created_at: '2026-06-30T10:00:00Z',
    ...overrides,
  };
}

function renderWorkspace(overrides: Partial<PipelineWorkspaceProps> = {}) {
  const flowTasks = overrides.flow?.tasks ?? [
    makeTask({ id: 'task-1', title: 'Visible task', status: 'IN_PROGRESS' }),
    makeTask({ id: 'task-2', title: 'Queued task', status: 'QUEUED' }),
  ];
  const canOperateProject = Object.prototype.hasOwnProperty.call(overrides, 'canOperateProject')
    ? overrides.canOperateProject as boolean
    : true;
  const props: PipelineWorkspaceProps = {
    canOperateProject,
    surface: {
      pipelineSection: 'flow',
      onPipelineSectionChange: vi.fn(),
      showArchive: false,
      setShowArchive: vi.fn(),
      viewMode: 'pipeline',
      setViewMode: vi.fn(),
      ...overrides.surface,
    },
    chrome: {
      actions: null,
      filterStrip: null,
      pagination: null,
      ...overrides.chrome,
    },
    archive: (overrides.archive ?? {}) as PipelineWorkspaceProps['archive'],
    flow: {
      tasks: flowTasks,
      hasActiveFilters: false,
      onViewLogs: vi.fn(),
      onNavigateToParent: vi.fn(),
      onArchiveGroup: vi.fn(),
      onUnarchiveTask: vi.fn(),
      ...overrides.flow,
    },
    rail: {
      currentTask: null,
      attentionTasks: [],
      localFallbackTasks: [],
      trackedTaskIds: new Set<string>(),
      taskContextSessionProps: {},
      onOpenTask: vi.fn(),
      onOpenPipeline: vi.fn(),
      onTrackTask: vi.fn(),
      ...overrides.rail,
    },
    board: {
      tasks: flowTasks,
      columns: [],
      loading: false,
      maxRetries: 3,
      actionLoadingMap: {},
      compact: false,
      sortBy: 'newest',
      selectionMode: false,
      selectedTaskIds: new Set<string>(),
      onToggleTaskSelection: vi.fn(),
      onCtrlClick: vi.fn(),
      onBulkToggle: vi.fn(),
      onShiftSelect: vi.fn(),
      onArchiveColumn: vi.fn(),
      onApprove: vi.fn(),
      onRequestChanges: vi.fn(),
      onCancel: vi.fn(),
      onRetry: vi.fn(),
      onEditPrompt: vi.fn(),
      onDismiss: vi.fn(),
      onArchive: vi.fn(),
      onViewLogs: vi.fn(),
      onTrack: vi.fn(),
      trackedTaskIds: new Set<string>(),
      onDuplicate: vi.fn(),
      hasActiveFilters: false,
      searchQuery: '',
      onDirectionClick: vi.fn(),
      ...overrides.board,
    },
    metrics: {
      activeTasks: [],
      queuedTasks: [],
      blockedTasks: [],
      failedTasks: [],
      terminalTasks: [],
      ...overrides.metrics,
    },
    status: overrides.status,
    stream: overrides.stream,
  };

  return render(<PipelineWorkspace {...props} />);
}

afterEach(() => {
  vi.useRealTimers();
  capturedKanbanProps = null;
});

describe('PipelineWorkspace access boundary', () => {
  test('captures a read-only board with inert mutation callbacks for viewers', async () => {
    const onApprove = vi.fn();
    const onBulkToggle = vi.fn();
    const onDuplicate = vi.fn();

    renderWorkspace({
      canOperateProject: false,
      surface: {
        pipelineSection: 'board',
        onPipelineSectionChange: vi.fn(),
        showArchive: false,
        setShowArchive: vi.fn(),
        viewMode: 'kanban',
        setViewMode: vi.fn(),
      },
      board: {
        onApprove,
        onBulkToggle,
        onDuplicate,
        selectionMode: true,
        selectedTaskIds: new Set(['task-1']),
      } as Partial<PipelineWorkspaceProps['board']> as PipelineWorkspaceProps['board'],
    });

    expect(await screen.findByTestId('kanban-board')).toBeInTheDocument();
    expect(capturedKanbanProps).toMatchObject({
      readOnly: true,
      selectionMode: false,
    });
    expect(capturedKanbanProps?.selectedTaskIds).toEqual(new Set());

    (capturedKanbanProps?.onApprove as (id: string) => void)('task-1');
    (capturedKanbanProps?.onBulkToggle as (ids: string[], selected: boolean) => void)(['task-1'], true);
    (capturedKanbanProps?.onDuplicate as (task: Task) => void)(makeTask());
    expect(onApprove).not.toHaveBeenCalled();
    expect(onBulkToggle).not.toHaveBeenCalled();
    expect(onDuplicate).not.toHaveBeenCalled();
  });

  test('fails closed when canOperateProject is undefined at runtime', async () => {
    const onApprove = vi.fn();
    renderWorkspace({
      canOperateProject: undefined as never,
      surface: {
        pipelineSection: 'board',
        onPipelineSectionChange: vi.fn(),
        showArchive: false,
        setShowArchive: vi.fn(),
        viewMode: 'kanban',
        setViewMode: vi.fn(),
      },
      board: { onApprove, selectionMode: true } as Partial<PipelineWorkspaceProps['board']> as PipelineWorkspaceProps['board'],
    });

    expect(await screen.findByTestId('kanban-board')).toBeInTheDocument();
    expect(capturedKanbanProps).toMatchObject({ readOnly: true, selectionMode: false });
    (capturedKanbanProps?.onApprove as (id: string) => void)('task-1');
    expect(onApprove).not.toHaveBeenCalled();
  });
});

describe('PipelineWorkspace status strip', () => {
  test('labels project scope and separates displayed, loaded, and server totals', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-30T12:00:30Z'));

    renderWorkspace({
      flow: {
        tasks: [
          makeTask({ id: 'task-1', title: 'Filtered active task', status: 'IN_PROGRESS' }),
          makeTask({ id: 'task-2', title: 'Filtered queued task', status: 'QUEUED' }),
        ],
        hasActiveFilters: true,
        onViewLogs: vi.fn(),
        onNavigateToParent: vi.fn(),
        onArchiveGroup: vi.fn(),
        onUnarchiveTask: vi.fn(),
      },
      status: {
        selectedProjectId: 'project-1',
        selectedProjectLabel: 'acme/widgets',
        displayedRows: 2,
        loadedRows: 7,
        serverTotalRows: 42,
        lastFetchTime: Date.parse('2026-06-30T12:00:00Z'),
        apiHealthy: true,
        wsConnected: true,
      },
    });

    expect(screen.getByRole('button', { name: 'Tasks' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Board' })).toBeInTheDocument();
    expect(screen.queryByTestId('pipeline-status-details')).not.toBeInTheDocument();
    expect(screen.getByText('Data health')).toBeInTheDocument();
    expect(screen.getByText('acme/widgets')).toHaveAttribute('title', 'Project-scoped task view: acme/widgets');
    expect(screen.getByText('2/7 shown')).toHaveAttribute('title', 'Rows currently displayed compared with the loaded page');
    expect(screen.getByText('fetched 30s ago')).toBeInTheDocument();
    expect(screen.getByText('API healthy')).toBeInTheDocument();
    expect(screen.getByText('Stream on')).toBeInTheDocument();

    fireEvent.click(screen.getByTitle('Show data details'));
    const details = screen.getByTestId('pipeline-status-details');
    expect(within(details).getByText('Scope')).toBeInTheDocument();
    expect(within(details).getByText('Displayed')).toBeInTheDocument();
    expect(within(details).getByText('Loaded')).toBeInTheDocument();
    expect(within(details).getByText('Server total')).toBeInTheDocument();
    expect(within(screen.getByTitle('Rows currently displayed after filters')).getByText('2')).toBeInTheDocument();
    expect(within(screen.getByTitle('Rows loaded in the current server page')).getByText('7')).toBeInTheDocument();
    expect(within(screen.getByTitle('Total rows reported by the server for this scope')).getByText('42')).toBeInTheDocument();
  });

  test('shows global stale state and omits websocket when not supplied', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-30T12:05:00Z'));

    renderWorkspace({
      status: {
        displayedRows: 2,
        loadedRows: 50,
        serverTotalRows: 150,
        lastFetchTime: Date.parse('2026-06-30T12:00:00Z'),
        apiHealthy: true,
        staleAfterMs: 60_000,
      },
    });

    expect(screen.getByText('All projects')).toHaveAttribute('title', 'Global task view');
    expect(screen.getByText('data stale 5m ago')).toHaveAttribute('title', 'Task data freshness from the existing poller state');
    expect(screen.getByText('API healthy')).toHaveAttribute('title', 'Task API health from the existing poller state');
    expect(screen.queryByText(/Stream (on|off)/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByTitle('Show data details'));
    expect(screen.getByText('Event stream')).toBeInTheDocument();
    expect(screen.getByText('not reported')).toBeInTheDocument();
  });

  test('keeps degraded API separate from fetch staleness and renders disconnected websocket state', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-30T12:05:00Z'));

    renderWorkspace({
      status: {
        displayedRows: 2,
        loadedRows: 50,
        serverTotalRows: 150,
        lastFetchTime: Date.parse('2026-06-30T12:00:00Z'),
        apiHealthy: false,
        wsConnected: false,
        staleAfterMs: 60_000,
      },
    });

    expect(screen.getByText('API degraded')).toBeInTheDocument();
    expect(screen.getByText('data stale 5m ago')).toBeInTheDocument();
    expect(screen.getByText('Stream off')).toBeInTheDocument();

    fireEvent.click(screen.getByTitle('Show data details'));
    expect(screen.getByText('disconnected')).toBeInTheDocument();
  });

  test('handles missing fetch time without overstating freshness', () => {
    renderWorkspace({
      status: {
        displayedRows: 0,
        loadedRows: 0,
        serverTotalRows: 0,
        lastFetchTime: 0,
        apiHealthy: true,
      },
    });

    expect(screen.getByText('fetch pending')).toBeInTheDocument();
    expect(screen.getByText('API healthy')).toBeInTheDocument();
  });

  test('renders bounded project scope in board mode', async () => {
    renderWorkspace({
      surface: {
        pipelineSection: 'board',
        onPipelineSectionChange: vi.fn(),
        showArchive: false,
        setShowArchive: vi.fn(),
        viewMode: 'kanban',
        setViewMode: vi.fn(),
      },
      status: {
        selectedProjectId: 'project-1',
        selectedProjectLabel: 'very-long-owner-name/very-long-repository-name-that-should-not-stretch-the-strip',
        displayedRows: 2,
        loadedRows: 2,
        serverTotalRows: 2,
        lastFetchTime: null,
        apiHealthy: true,
        wsConnected: true,
      },
    });

    expect(await screen.findByTestId('kanban-board')).toBeInTheDocument();
    expect(screen.getByText('very-long-owner-name/very-long-repository-name-that-should-not-stretch-the-strip'))
      .toHaveAttribute(
        'title',
        'Project-scoped task view: very-long-owner-name/very-long-repository-name-that-should-not-stretch-the-strip',
      );
  });
});

describe('PipelineWorkspace section routing', () => {
  test('renders archive from the selected section even when the legacy archive flag is stale', async () => {
    renderWorkspace({
      surface: {
        pipelineSection: 'archive',
        onPipelineSectionChange: vi.fn(),
        showArchive: false,
        setShowArchive: vi.fn(),
        viewMode: 'pipeline',
        setViewMode: vi.fn(),
      },
    });

    expect(screen.getByRole('button', { name: 'Archive' })).toHaveAttribute('aria-pressed', 'true');
    expect(await screen.findByTestId('archive-view')).toBeInTheDocument();
    expect(screen.queryByTestId('pipeline-view')).not.toBeInTheDocument();
    expect(screen.queryByTestId('kanban-board')).not.toBeInTheDocument();
  });

  test('archive close returns to task flow and synchronizes legacy view state', () => {
    const onPipelineSectionChange = vi.fn();
    const setShowArchive = vi.fn();
    const setViewMode = vi.fn();
    renderWorkspace({
      surface: {
        pipelineSection: 'archive',
        onPipelineSectionChange,
        showArchive: true,
        setShowArchive,
        viewMode: 'pipeline',
        setViewMode,
      },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Close archive mock' }));

    expect(setShowArchive).toHaveBeenCalledWith(false);
    expect(onPipelineSectionChange).toHaveBeenCalledWith('flow');
    expect(setViewMode).toHaveBeenCalledWith('pipeline');
  });

  test('renders board from the selected section even when the legacy view mode is stale', () => {
    renderWorkspace({
      surface: {
        pipelineSection: 'board',
        onPipelineSectionChange: vi.fn(),
        showArchive: false,
        setShowArchive: vi.fn(),
        viewMode: 'pipeline',
        setViewMode: vi.fn(),
      },
    });

    expect(screen.getByRole('button', { name: 'Board' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('kanban-board')).toBeInTheDocument();
    expect(screen.queryByTestId('pipeline-view')).not.toBeInTheDocument();
    expect(screen.queryByTestId('archive-view')).not.toBeInTheDocument();
  });

  test('renders tasks from the selected section even when the legacy view mode is stale', () => {
    const trackedTask = makeTask({ id: 'task-stale-flow-stream', title: 'Tracked stale flow task', status: 'IN_PROGRESS' });
    renderWorkspace({
      surface: {
        pipelineSection: 'flow',
        onPipelineSectionChange: vi.fn(),
        showArchive: true,
        setShowArchive: vi.fn(),
        viewMode: 'kanban',
        setViewMode: vi.fn(),
      },
      flow: {
        tasks: [trackedTask],
        hasActiveFilters: false,
        onViewLogs: vi.fn(),
        onNavigateToParent: vi.fn(),
        onArchiveGroup: vi.fn(),
        onUnarchiveTask: vi.fn(),
      },
      stream: {
        trackedTaskIds: new Set([trackedTask.id]),
        trackedLogs: new Map([[trackedTask.id, [{ id: 1, t: '2026-06-30T12:00:01Z', type: 'system', msg: 'stream attached' }]]]),
        tasks: [trackedTask],
        onUntrack: vi.fn(),
        onUntrackAll: vi.fn(),
      },
    });

    expect(screen.getByRole('button', { name: 'Tasks' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('pipeline-view')).toBeInTheDocument();
    expect(screen.getByTestId('task-stream-panel')).toHaveAttribute('data-variant', 'embedded');
    expect(screen.queryByTestId('kanban-board')).not.toBeInTheDocument();
    expect(screen.queryByTestId('archive-view')).not.toBeInTheDocument();
  });
});

describe('PipelineWorkspace task stream', () => {
  test('composes pipeline metrics, embedded stream, and task context in the flow sideboard', () => {
    const trackedTask = makeTask({ id: 'task-stream-1', title: 'Tracked pipeline task', status: 'IN_PROGRESS' });

    renderWorkspace({
      flow: {
        tasks: [trackedTask],
        hasActiveFilters: true,
        onViewLogs: vi.fn(),
        onNavigateToParent: vi.fn(),
        onArchiveGroup: vi.fn(),
        onUnarchiveTask: vi.fn(),
      },
      metrics: {
        activeTasks: [trackedTask],
        queuedTasks: [makeTask({ id: 'queued-task', status: 'QUEUED' })],
        blockedTasks: [],
        failedTasks: [],
        terminalTasks: [makeTask({ id: 'done-task', status: 'COMPLETED' })],
      },
      rail: {
        currentTask: trackedTask,
        attentionTasks: [],
        localFallbackTasks: [],
        trackedTaskIds: new Set([trackedTask.id]),
        taskContextSessionProps: {},
        onOpenTask: vi.fn(),
        onOpenPipeline: vi.fn(),
        onTrackTask: vi.fn(),
      },
      stream: {
        trackedTaskIds: new Set([trackedTask.id]),
        trackedLogs: new Map([[trackedTask.id, [{ id: 1, t: '2026-06-30T12:00:01Z', type: 'system', msg: 'stream attached' }]]]),
        tasks: [trackedTask],
        onUntrack: vi.fn(),
        onUntrackAll: vi.fn(),
      },
    });

    const sideboard = screen.getByTestId('pipeline-sideboard');
    const nowPanel = within(sideboard).getByTestId('pipeline-now-panel');
    expect(nowPanel).toBeInTheDocument();
    expect(within(nowPanel).getByText('Now')).toBeInTheDocument();
    expect(within(nowPanel).getByText('1 running')).toBeInTheDocument();
    expect(within(nowPanel).getByText('1 tracked')).toBeInTheDocument();
    expect(within(screen.getByTestId('pipeline-now-row-task-stream-1')).getByText('Selected')).toBeInTheDocument();
    const healthPanel = within(sideboard).getByTestId('pipeline-health-panel');
    expect(within(healthPanel).getByText('Task health')).toBeInTheDocument();
    expect(within(healthPanel).getByText('filtered')).toBeInTheDocument();
    expect(within(healthPanel).getByText('Running')).toBeInTheDocument();
    expect(within(healthPanel).getByText('Queued')).toBeInTheDocument();
    expect(within(healthPanel).getByText('Waiting')).toBeInTheDocument();
    expect(within(healthPanel).getByText('Parked')).toBeInTheDocument();
    expect(within(healthPanel).getByText('Finished')).toBeInTheDocument();
    expect(within(healthPanel).getByText('clear')).toBeInTheDocument();
    expect(within(sideboard).getByTestId('task-stream-panel')).toHaveAttribute('data-variant', 'embedded');
    expect(within(sideboard).getByTestId('task-context-rail')).toBeInTheDocument();

    const sideboardShell = sideboard.closest('aside');
    expect(sideboardShell).toHaveClass('max-h-[34vh]');
    expect(sideboardShell).toHaveClass('lg:col-start-2');
    expect(sideboardShell).toHaveClass('lg:border-l');
  });

  test('routes Now panel task open and track actions', () => {
    const onOpenTask = vi.fn();
    const onTrackTask = vi.fn();
    const activeTask = makeTask({ id: 'task-now-active', title: 'Active implementation task', status: 'IN_PROGRESS' });
    const blockedTask = makeTask({ id: 'task-now-blocked', title: 'Blocked integration task', status: 'BLOCKED' });
    const parkedTask = makeTask({ id: 'task-now-parked', title: 'Parked follow-up task', status: 'PARKED' });
    const queuedTask = makeTask({ id: 'task-now-queued', title: 'Queued review task', status: 'QUEUED' });

    renderWorkspace({
      flow: {
        tasks: [activeTask, blockedTask, parkedTask, queuedTask],
        hasActiveFilters: false,
        onViewLogs: vi.fn(),
        onNavigateToParent: vi.fn(),
        onArchiveGroup: vi.fn(),
        onUnarchiveTask: vi.fn(),
      },
      metrics: {
        activeTasks: [activeTask],
        queuedTasks: [queuedTask],
        blockedTasks: [blockedTask],
        failedTasks: [],
        terminalTasks: [],
      },
      rail: {
        currentTask: null,
        attentionTasks: [blockedTask],
        localFallbackTasks: [queuedTask],
        trackedTaskIds: new Set([activeTask.id]),
        taskContextSessionProps: {},
        onOpenTask,
        onOpenPipeline: vi.fn(),
        onTrackTask,
      },
    });

    const nowPanel = screen.getByTestId('pipeline-now-panel');
    expect(within(nowPanel).getByText('Now')).toBeInTheDocument();
    expect(within(nowPanel).getByText('1 running')).toBeInTheDocument();
    expect(within(nowPanel).getByText('1 tracked')).toBeInTheDocument();
    expect(within(screen.getByTestId('pipeline-now-row-task-now-active')).getByText('Running')).toBeInTheDocument();
    expect(within(screen.getByTestId('pipeline-now-row-task-now-blocked')).getByText('Blocked')).toBeInTheDocument();
    expect(within(screen.getByTestId('pipeline-now-row-task-now-parked')).getByText('Parked')).toBeInTheDocument();
    expect(within(screen.getByTestId('pipeline-now-row-task-now-queued')).getByText('Queued')).toBeInTheDocument();
    expect(screen.queryByLabelText('Track Active implementation task')).not.toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('Open Active implementation task'));
    expect(onOpenTask).toHaveBeenCalledWith(activeTask);

    fireEvent.click(screen.getByLabelText('Track Blocked integration task'));
    expect(onTrackTask).toHaveBeenCalledWith(blockedTask);
  });

  test('shows a quiet Now panel empty state when no active work is visible', () => {
    renderWorkspace({
      flow: {
        tasks: [],
        hasActiveFilters: false,
        onViewLogs: vi.fn(),
        onNavigateToParent: vi.fn(),
        onArchiveGroup: vi.fn(),
        onUnarchiveTask: vi.fn(),
      },
      metrics: {
        activeTasks: [],
        queuedTasks: [],
        blockedTasks: [],
        failedTasks: [],
        terminalTasks: [],
      },
    });

    const nowPanel = screen.getByTestId('pipeline-now-panel');
    expect(within(nowPanel).getByText('No active pipeline work in view.')).toBeInTheDocument();
  });

  test('counts parked-only tasks as attention in task health', () => {
    const parkedTask = makeTask({ id: 'task-health-parked', title: 'Parked task', status: 'PARKED' });

    renderWorkspace({
      flow: {
        tasks: [parkedTask],
        hasActiveFilters: false,
        onViewLogs: vi.fn(),
        onNavigateToParent: vi.fn(),
        onArchiveGroup: vi.fn(),
        onUnarchiveTask: vi.fn(),
      },
      metrics: {
        activeTasks: [],
        queuedTasks: [],
        blockedTasks: [],
        failedTasks: [],
        terminalTasks: [],
      },
    });

    const healthPanel = screen.getByTestId('pipeline-health-panel');
    expect(within(healthPanel).getByText('1 need attention')).toBeInTheDocument();
    expect(within(healthPanel).getByText('Parked')).toBeInTheDocument();
    expect(within(healthPanel).getByText('1')).toBeInTheDocument();
    expect(screen.queryByText('clear')).not.toBeInTheDocument();
  });

  test('separates queued dependency waits from blocked attention', () => {
    const readyQueuedTask = makeTask({ id: 'task-health-queued', title: 'Ready queued task', status: 'QUEUED' });
    const waitingTask = makeTask({
      id: 'task-health-waiting',
      title: 'Waiting dependency task',
      status: 'QUEUED',
      depends_on: ['task-upstream'],
    });

    renderWorkspace({
      flow: {
        tasks: [readyQueuedTask, waitingTask],
        hasActiveFilters: false,
        onViewLogs: vi.fn(),
        onNavigateToParent: vi.fn(),
        onArchiveGroup: vi.fn(),
        onUnarchiveTask: vi.fn(),
      },
      metrics: {
        activeTasks: [],
        queuedTasks: [readyQueuedTask, waitingTask],
        blockedTasks: [],
        failedTasks: [],
        terminalTasks: [],
      },
    });

    const healthPanel = screen.getByTestId('pipeline-health-panel');
    const healthRows = Array.from(healthPanel.querySelectorAll('div'));
    expect(healthRows.find(row => row.textContent?.includes('Queued'))?.textContent).toContain('1');
    expect(healthRows.find(row => row.textContent?.includes('Waiting'))?.textContent).toContain('1');
    expect(healthRows.find(row => row.textContent?.includes('Blocked'))?.textContent).toContain('0');
    expect(within(healthPanel).getByText('clear')).toBeInTheDocument();
    expect(within(screen.getByTestId('pipeline-now-row-task-health-waiting')).getByText('Waiting')).toBeInTheDocument();
    expect(within(screen.getByTestId('pipeline-now-row-task-health-waiting')).getByText('1 upstream')).toBeInTheDocument();
    expect(within(screen.getByTestId('pipeline-now-row-task-health-queued')).getByText('Queued')).toBeInTheDocument();
  });

  test('includes backend attention entries in Now and task health summaries', () => {
    const staleTask = makeTask({
      id: 'task-stale-provision',
      title: 'Stale provisioning task',
      status: 'PROVISIONING',
      latest_status_message: 'Raw provisioning status.',
    });

    renderWorkspace({
      flow: {
        tasks: [staleTask],
        hasActiveFilters: false,
        onViewLogs: vi.fn(),
        onNavigateToParent: vi.fn(),
        onArchiveGroup: vi.fn(),
        onUnarchiveTask: vi.fn(),
      },
      rail: {
        currentTask: null,
        attentionTasks: [{
          task: staleTask,
          attention: {
            id: 'task:task-stale-provision:stale_provisioning_task',
            kind: 'task',
            severity: 'needs_operator',
            reason: 'stale_provisioning_task',
            label: 'Provisioning stale',
            detail: 'VM was dispatched but never claimed.',
            ref: { kind: 'task', id: staleTask.id, project_id: 'proj-1' },
          },
        }],
        localFallbackTasks: [],
        trackedTaskIds: new Set<string>(),
        taskContextSessionProps: {},
        onOpenTask: vi.fn(),
        onOpenPipeline: vi.fn(),
        onTrackTask: vi.fn(),
      },
      metrics: {
        activeTasks: [],
        queuedTasks: [],
        blockedTasks: [],
        failedTasks: [],
        terminalTasks: [],
      },
    });

    const nowPanel = screen.getByTestId('pipeline-now-panel');
    expect(within(nowPanel).getByText('Provisioning stale')).toBeInTheDocument();
    expect(within(nowPanel).getByText('Stale provisioning task')).toBeInTheDocument();
    expect(within(nowPanel).getByText('VM was dispatched but never claimed.')).toBeInTheDocument();

    const healthPanel = screen.getByTestId('pipeline-health-panel');
    expect(within(healthPanel).getByText('1 need attention')).toBeInTheDocument();
    expect(screen.queryByText('clear')).not.toBeInTheDocument();
  });

  test('does not render embedded stream when no tasks are tracked', () => {
    renderWorkspace({
      stream: {
        trackedTaskIds: new Set<string>(),
        trackedLogs: new Map<string, TrackLogEntry[]>(),
        tasks: [],
        onUntrack: vi.fn(),
        onUntrackAll: vi.fn(),
      },
    });

    expect(screen.queryByTestId('task-stream-panel')).not.toBeInTheDocument();
  });

  test('renders embedded stream with tracked task title and log count', () => {
    const trackedTask = makeTask({ id: 'task-stream-1', title: 'Tracked pipeline task', status: 'IN_PROGRESS' });
    const logs: TrackLogEntry[] = [
      { id: 1, t: '2026-06-30T12:00:01Z', type: 'system', msg: 'stream attached' },
      { id: 2, t: '2026-06-30T12:00:02Z', type: 'log', msg: 'agent started' },
    ];

    renderWorkspace({
      stream: {
        trackedTaskIds: new Set([trackedTask.id]),
        trackedLogs: new Map([[trackedTask.id, logs]]),
        tasks: [trackedTask],
        onUntrack: vi.fn(),
        onUntrackAll: vi.fn(),
      },
    });

    const panel = screen.getByTestId('task-stream-panel');
    expect(panel).toHaveAttribute('data-variant', 'embedded');
    expect(panel).not.toHaveClass('fixed');
    expect(screen.getByText('Tracked pipeline task')).toBeInTheDocument();
    expect(screen.getByText('2 entries')).toBeInTheDocument();
    expect(screen.getByText('agent started')).toBeInTheDocument();
  });

  test('renders embedded stream waiting state', () => {
    const trackedTask = makeTask({ id: 'task-stream-2', title: 'Waiting pipeline task', status: 'PROVISIONING' });

    renderWorkspace({
      stream: {
        trackedTaskIds: new Set([trackedTask.id]),
        trackedLogs: new Map<string, TrackLogEntry[]>(),
        tasks: [trackedTask],
        onUntrack: vi.fn(),
        onUntrackAll: vi.fn(),
      },
    });

    expect(screen.getByTestId('task-stream-panel')).toHaveAttribute('data-variant', 'embedded');
    expect(screen.getByText('Waiting pipeline task')).toBeInTheDocument();
    expect(screen.getByText('Waiting for events')).toBeInTheDocument();
    expect(screen.getByText('0 entries')).toBeInTheDocument();
  });

  test('keeps an untrackable id fallback when the tracked task is not loaded', () => {
    const onUntrack = vi.fn();

    renderWorkspace({
      stream: {
        trackedTaskIds: new Set(['missing-tracked-task']),
        trackedLogs: new Map<string, TrackLogEntry[]>([
          ['missing-tracked-task', [{ id: 1, t: '2026-06-30T12:00:01Z', type: 'system', msg: 'still tracking' }]],
        ]),
        tasks: [],
        onUntrack,
        onUntrackAll: vi.fn(),
      },
    });

    expect(screen.getByText('missing-')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Untrack'));
    expect(onUntrack).toHaveBeenCalledWith('missing-tracked-task');
  });
});
