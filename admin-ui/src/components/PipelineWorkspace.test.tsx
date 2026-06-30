import { afterEach, describe, expect, test, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { PipelineWorkspace, type PipelineWorkspaceProps } from './PipelineWorkspace';
import type { Task } from './TaskCard';
import type { TrackLogEntry } from '../types/task';

vi.mock('./ArchiveView', () => ({
  default: () => <div data-testid="archive-view" />,
}));

vi.mock('./PipelineView', () => ({
  PipelineView: () => <div data-testid="pipeline-view" />,
}));

vi.mock('./KanbanBoard', () => ({
  KanbanBoard: () => <div data-testid="kanban-board" />,
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
  const props: PipelineWorkspaceProps = {
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

    expect(screen.getByText('Project: acme/widgets')).toHaveAttribute('title', 'Project-scoped task view: acme/widgets');
    expect(screen.getByText('displayed 2')).toHaveAttribute('title', 'Rows currently displayed after filters');
    expect(screen.getByText('loaded 7')).toHaveAttribute('title', 'Rows loaded in the current server page');
    expect(screen.getByText('server 42')).toHaveAttribute('title', 'Total rows reported by the server for this scope');
    expect(screen.getByText('fetched 30s ago')).toBeInTheDocument();
    expect(screen.getByText('API healthy')).toBeInTheDocument();
    expect(screen.getByText('WS connected')).toBeInTheDocument();
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

    expect(screen.getByText('Global')).toHaveAttribute('title', 'Global task view');
    expect(screen.getByText('data stale 5m ago')).toHaveAttribute('title', 'Task data freshness from the existing poller state');
    expect(screen.getByText('API healthy')).toHaveAttribute('title', 'Task API health from the existing poller state');
    expect(screen.queryByText(/WS /)).not.toBeInTheDocument();
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
    expect(screen.getByText('WS disconnected')).toBeInTheDocument();
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

  test('renders bounded project scope in board mode', () => {
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

    expect(screen.getByTestId('kanban-board')).toBeInTheDocument();
    expect(screen.getByText('Project: very-long-owner-name/very-long-repository-name-that-should-not-stretch-the-strip'))
      .toHaveAttribute(
        'title',
        'Project-scoped task view: very-long-owner-name/very-long-repository-name-that-should-not-stretch-the-strip',
      );
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
    expect(within(sideboard).getByText('active')).toBeInTheDocument();
    expect(within(sideboard).getByText('queued')).toBeInTheDocument();
    expect(within(sideboard).getByText('terminal')).toBeInTheDocument();
    expect(within(sideboard).getByTestId('task-stream-panel')).toHaveAttribute('data-variant', 'embedded');
    expect(within(sideboard).getByTestId('task-context-rail')).toBeInTheDocument();

    const sideboardShell = sideboard.closest('aside');
    expect(sideboardShell).toHaveClass('max-h-[34vh]');
    expect(sideboardShell).toHaveClass('lg:col-start-2');
    expect(sideboardShell).toHaveClass('lg:border-l');
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
