import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { fireEvent } from '@testing-library/react';
import { EvergreenFillObservabilityPanel } from './EvergreenFillObservabilityPanel';
import { __pollerInternals } from '../hooks/usePoller';

const mockFetchEvergreenFillObservability = vi.fn();

vi.mock('../api', () => ({
  fetchEvergreenFillObservability: (...args: unknown[]) => mockFetchEvergreenFillObservability(...args),
}));

const OBSERVABILITY_RESPONSE = {
  created_count: 5,
  reason_counts: { util_low: 3, backlog_ready: 2 },
  trigger_counts: { scheduled: 4, manual: 1 },
  recent: [
    { action: 'skip', reason: 'util_high', trigger: 'scheduled', project_id: 'proj-abc-1234' },
    { action: 'create', reason: 'util_low', trigger: 'manual', project_id: null },
  ],
};

beforeEach(() => {
  mockFetchEvergreenFillObservability.mockResolvedValue(OBSERVABILITY_RESPONSE);
});

afterEach(() => {
  __pollerInternals.reset();
  vi.clearAllMocks();
});

describe('EvergreenFillObservabilityPanel', () => {
  it('renders the panel header', () => {
    render(<EvergreenFillObservabilityPanel />);
    expect(screen.getByText('Fill Observability')).toBeInTheDocument();
  });

  it('shows created count after load', async () => {
    render(<EvergreenFillObservabilityPanel />);
    await waitFor(() => expect(screen.getByText('5 created')).toBeInTheDocument());
  });

  it('renders reason counts', async () => {
    render(<EvergreenFillObservabilityPanel />);
    await waitFor(() => expect(screen.getAllByText('util_low').length).toBeGreaterThan(0));
    expect(screen.getByText('backlog_ready')).toBeInTheDocument();
    expect(screen.getByText('Reason Counts')).toBeInTheDocument();
  });

  it('renders trigger counts', async () => {
    render(<EvergreenFillObservabilityPanel />);
    await waitFor(() => expect(screen.getAllByText('scheduled').length).toBeGreaterThan(0));
    expect(screen.getByText('Trigger Counts')).toBeInTheDocument();
  });

  it('renders recent decisions table', async () => {
    render(<EvergreenFillObservabilityPanel />);
    await waitFor(() => expect(screen.getByText('skip')).toBeInTheDocument());
    expect(screen.getByText('create')).toBeInTheDocument();
    expect(screen.getByText('util_high')).toBeInTheDocument();
    expect(screen.getByText('Recent Decisions')).toBeInTheDocument();
  });

  it('collapses and hides content on header click', async () => {
    render(<EvergreenFillObservabilityPanel />);
    await waitFor(() => expect(screen.getByText('Reason Counts')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('evergreen-fill-observability-panel').querySelector('button')!);
    expect(screen.queryByText('Reason Counts')).not.toBeInTheDocument();
  });

  it('shows error message on fetch failure', async () => {
    mockFetchEvergreenFillObservability.mockRejectedValue(new Error('Network error'));
    render(<EvergreenFillObservabilityPanel />);
    await waitFor(() => expect(screen.getByText('Network error')).toBeInTheDocument());
  });

  it('shows empty state when no decisions recorded', async () => {
    mockFetchEvergreenFillObservability.mockResolvedValue({
      created_count: 0,
      reason_counts: {},
      trigger_counts: {},
      recent: [],
    });
    render(<EvergreenFillObservabilityPanel />);
    await waitFor(() => expect(screen.getByText('No fill decisions recorded yet.')).toBeInTheDocument());
  });
});
