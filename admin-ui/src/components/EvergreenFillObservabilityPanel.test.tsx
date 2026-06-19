import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { fireEvent } from '@testing-library/react';
import { EvergreenFillObservabilityPanel } from './EvergreenFillObservabilityPanel';
import { __pollerInternals } from '../hooks/usePoller';

const mockFetchEvergreenFillObservability = vi.fn();
const mockFetchObserveModeVerdict = vi.fn();

vi.mock('../api', () => ({
  fetchEvergreenFillObservability: (...args: unknown[]) => mockFetchEvergreenFillObservability(...args),
  fetchObserveModeVerdict: (...args: unknown[]) => mockFetchObserveModeVerdict(...args),
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

const VERDICT_RESPONSE_GO = {
  verdict: 'go' as const,
  verdict_reason: 'Cap-adjusted fire rate 8.5% is within threshold 20.0%',
  window_hours: 168,
  observe_evaluations: 47,
  would_have_fired: 4,
  fire_rate_pct: 8.5,
  max_fire_rate_pct: 20.0,
  min_evaluations: 10,
  daily_cap: 3,
  cap_adjusted_would_have_fired: 4,
  cap_adjusted_fire_rate_pct: 8.5,
};

const VERDICT_RESPONSE_NO_GO = {
  verdict: 'no_go' as const,
  verdict_reason: 'Cap-adjusted fire rate 25.3% exceeds threshold 20.0%',
  window_hours: 168,
  observe_evaluations: 59,
  would_have_fired: 15,
  fire_rate_pct: 25.3,
  max_fire_rate_pct: 20.0,
  min_evaluations: 10,
  daily_cap: 3,
  cap_adjusted_would_have_fired: 15,
  cap_adjusted_fire_rate_pct: 25.3,
};

const VERDICT_RESPONSE_INSUFFICIENT = {
  verdict: 'insufficient_data' as const,
  verdict_reason: 'Only 5 evaluations recorded, need at least 10',
  window_hours: 168,
  observe_evaluations: 5,
  would_have_fired: 0,
  fire_rate_pct: null,
  max_fire_rate_pct: 20.0,
  min_evaluations: 10,
  daily_cap: 3,
  cap_adjusted_would_have_fired: 0,
  cap_adjusted_fire_rate_pct: null,
};

beforeEach(() => {
  mockFetchEvergreenFillObservability.mockResolvedValue(OBSERVABILITY_RESPONSE);
  mockFetchObserveModeVerdict.mockResolvedValue(VERDICT_RESPONSE_GO);
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

  it('renders verdict badge with go state', async () => {
    render(<EvergreenFillObservabilityPanel />);
    await waitFor(() => expect(screen.getByText('✓ GO')).toBeInTheDocument());
    expect(screen.getByText('Observe Mode Verdict')).toBeInTheDocument();
  });

  it('displays verdict reason', async () => {
    render(<EvergreenFillObservabilityPanel />);
    await waitFor(() => expect(screen.getByText('Cap-adjusted fire rate 8.5% is within threshold 20.0%')).toBeInTheDocument());
  });

  it('displays cap-adjusted and raw fire rate percentages', async () => {
    render(<EvergreenFillObservabilityPanel />);
    await waitFor(() => {
      const container = screen.getByTestId('evergreen-fill-observability-panel');
      expect(container.textContent).toMatch(/Cap-Adjusted Fire Rate:\s*8\.5%/);
      expect(container.textContent).toMatch(/Raw Fire Rate:\s*8\.5%/);
    });
  });

  it('displays evaluation counts', async () => {
    render(<EvergreenFillObservabilityPanel />);
    await waitFor(() => {
      expect(screen.getByText(/Evaluations:/)).toBeInTheDocument();
      expect(screen.getByText(/would have fired: 4/)).toBeInTheDocument();
    });
  });

  it('renders no_go verdict badge', async () => {
    mockFetchObserveModeVerdict.mockResolvedValue(VERDICT_RESPONSE_NO_GO);
    render(<EvergreenFillObservabilityPanel />);
    await waitFor(() => expect(screen.getByText('✗ NO-GO')).toBeInTheDocument());
  });

  it('renders insufficient_data verdict badge', async () => {
    mockFetchObserveModeVerdict.mockResolvedValue(VERDICT_RESPONSE_INSUFFICIENT);
    render(<EvergreenFillObservabilityPanel />);
    await waitFor(() => expect(screen.getByText('◯ INSUFFICIENT DATA')).toBeInTheDocument());
    expect(screen.getByText('Only 5 evaluations recorded, need at least 10')).toBeInTheDocument();
  });

  it('shows null fire_rate as empty', async () => {
    mockFetchObserveModeVerdict.mockResolvedValue(VERDICT_RESPONSE_INSUFFICIENT);
    render(<EvergreenFillObservabilityPanel />);
    await waitFor(() => expect(screen.getByText('◯ INSUFFICIENT DATA')).toBeInTheDocument());
    expect(screen.queryByText(/Fire Rate/)).not.toBeInTheDocument();
  });

  it('shows verdict error message on fetch failure', async () => {
    mockFetchObserveModeVerdict.mockRejectedValue(new Error('Verdict fetch failed'));
    render(<EvergreenFillObservabilityPanel />);
    await waitFor(() => expect(screen.getByText(/Verdict load error.*Verdict fetch failed/)).toBeInTheDocument());
  });

  it('collapses verdict section with rest of panel', async () => {
    render(<EvergreenFillObservabilityPanel />);
    await waitFor(() => expect(screen.getByText('Observe Mode Verdict')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('evergreen-fill-observability-panel').querySelector('button')!);
    expect(screen.queryByText('Observe Mode Verdict')).not.toBeInTheDocument();
  });
});
