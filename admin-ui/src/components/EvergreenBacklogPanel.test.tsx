import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { EvergreenBacklogPanel } from './EvergreenBacklogPanel';
import { __pollerInternals } from '../hooks/usePoller';
const mockFetchEvergreenBacklog = vi.fn();
const mockFetchEvergreenBacklogSummary = vi.fn();

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    fetchEvergreenBacklog: (...args: unknown[]) => mockFetchEvergreenBacklog(...args),
    fetchEvergreenBacklogSummary: (...args: unknown[]) => mockFetchEvergreenBacklogSummary(...args),
  });
});

vi.mock('../utils/time', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/time')>()),
  formatAbsolute: (v: string) => `abs:${v}`,
}));

const BACKLOG_ITEMS = [
  {
    id: 'item-1',
    project_id: 'proj-1',
    finding_key: 'fix-null-check',
    title: 'Fix null pointer in auth',
    detail: null,
    status: 'enrolled',
    outcome: null,
    provenance: 'research',
    direction_id: null,
    attempts: 0,
    spawned_task_id: null,
    verified_against_sha: null,
    created_at: '2026-06-01T10:00:00Z',
    updated_at: '2026-06-01T10:00:00Z',
  },
  {
    id: 'item-2',
    project_id: 'proj-1',
    finding_key: 'add-retry-logic',
    title: 'Add retry logic to payments',
    detail: 'Should retry 3 times',
    status: 'spawned',
    outcome: null,
    provenance: 'deferred_memo',
    direction_id: 'd-abc',
    attempts: 1,
    spawned_task_id: 'task-abcdef-1234',
    verified_against_sha: null,
    created_at: '2026-06-02T10:00:00Z',
    updated_at: '2026-06-02T12:00:00Z',
  },
];

const SUMMARY = { enrolled: 1, spawned: 1, done: 0, retired: 0 };

beforeEach(() => {
  mockFetchEvergreenBacklog.mockResolvedValue({ items: BACKLOG_ITEMS });
  mockFetchEvergreenBacklogSummary.mockResolvedValue(SUMMARY);
});

afterEach(() => {
  __pollerInternals.reset();
  vi.clearAllMocks();
});

describe('EvergreenBacklogPanel', () => {
  it('renders the panel header', () => {
    render(<EvergreenBacklogPanel projectId="proj-1" />);
    expect(screen.getByText('Evergreen Backlog')).toBeInTheDocument();
  });

  it('shows item count after load', async () => {
    render(<EvergreenBacklogPanel projectId="proj-1" />);
    await waitFor(() => expect(screen.getByText('2 items')).toBeInTheDocument());
  });

  it('renders backlog item titles', async () => {
    render(<EvergreenBacklogPanel projectId="proj-1" />);
    await waitFor(() => expect(screen.getByText('Fix null pointer in auth')).toBeInTheDocument());
    expect(screen.getByText('Add retry logic to payments')).toBeInTheDocument();
  });

  it('renders status badges', async () => {
    render(<EvergreenBacklogPanel projectId="proj-1" />);
    await waitFor(() => expect(screen.getAllByText('enrolled').length).toBeGreaterThan(0));
    expect(screen.getAllByText('spawned').length).toBeGreaterThan(0);
  });

  it('renders provenance column', async () => {
    render(<EvergreenBacklogPanel projectId="proj-1" />);
    await waitFor(() => expect(screen.getByText('research')).toBeInTheDocument());
    expect(screen.getByText('deferred_memo')).toBeInTheDocument();
  });

  it('renders truncated spawned_task_id', async () => {
    render(<EvergreenBacklogPanel projectId="proj-1" />);
    await waitFor(() => expect(screen.getByText('task-abc…')).toBeInTheDocument());
  });

  it('renders summary counts', async () => {
    render(<EvergreenBacklogPanel projectId="proj-1" />);
    await waitFor(() => {
      const enrolledBadges = screen.getAllByText('enrolled');
      expect(enrolledBadges.length).toBeGreaterThan(0);
    });
  });

  it('shows "Select a project" prompt when projectId is null', () => {
    render(<EvergreenBacklogPanel projectId={null} />);
    expect(screen.getByText('Select a project to view its backlog.')).toBeInTheDocument();
  });

  it('collapses content on header click', async () => {
    render(<EvergreenBacklogPanel projectId="proj-1" />);
    await waitFor(() => expect(screen.getByText('Fix null pointer in auth')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Evergreen Backlog'));
    expect(screen.queryByText('Fix null pointer in auth')).not.toBeInTheDocument();
  });

  it('shows error message on fetch failure', async () => {
    mockFetchEvergreenBacklog.mockRejectedValue(new Error('Connection refused'));
    render(<EvergreenBacklogPanel projectId="proj-1" />);
    await waitFor(() => expect(screen.getByText('Connection refused')).toBeInTheDocument());
  });

  it('shows empty state when backlog is empty', async () => {
    mockFetchEvergreenBacklog.mockResolvedValue({ items: [] });
    mockFetchEvergreenBacklogSummary.mockResolvedValue({});
    render(<EvergreenBacklogPanel projectId="proj-1" />);
    await waitFor(() => expect(screen.getByText('No backlog items.')).toBeInTheDocument());
  });

  it('passes projectId to fetch calls', async () => {
    render(<EvergreenBacklogPanel projectId="proj-xyz" />);
    await waitFor(() => expect(mockFetchEvergreenBacklog).toHaveBeenCalledWith('proj-xyz', expect.anything()));
    expect(mockFetchEvergreenBacklogSummary).toHaveBeenCalledWith('proj-xyz', expect.anything());
  });
});
