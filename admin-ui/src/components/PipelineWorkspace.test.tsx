import { afterEach, describe, expect, test, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { PipelineWorkspace, type PipelineWorkspaceProps } from './PipelineWorkspace';
import type { Task } from './TaskCard';

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
