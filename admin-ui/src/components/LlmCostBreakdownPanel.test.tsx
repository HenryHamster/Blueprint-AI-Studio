import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import { LlmCostBreakdownPanel } from './LlmCostBreakdownPanel';
import type { LlmCostBreakdownResponse } from '../api';

const { fetchLlmCostBreakdownMock } = vi.hoisted(() => ({
  fetchLlmCostBreakdownMock: vi.fn(),
}));

vi.mock('../api', () => ({
  fetchLlmCostBreakdown: (...args: unknown[]) => fetchLlmCostBreakdownMock(...args),
}));

function makeResponse(overrides: Partial<LlmCostBreakdownResponse> = {}): LlmCostBreakdownResponse {
  return {
    window_days: 7,
    generated_at: '2026-06-12T00:00:00Z',
    by_provider: [],
    by_model: [
      {
        provider: 'anthropic',
        model: 'claude-sonnet-4-6',
        total_input_tokens: 100_000,
        total_output_tokens: 10_000,
        total_cost_usd: 2.5,
        mean_cost_per_task: 0.5,
        llm_call_count: 15,
        task_count: 5,
        merged_count: 3,
        merge_rate: 0.6,
        verified_merged_count: 3,
        verified_merge_rate: 0.6,
        cost_per_merge: 0.83,
        share_of_total_cost: 1.0,
        priced: true,
        approximate_priced: false,
      },
    ],
    by_task_type: [],
    totals: {
      total_input_tokens: 100_000,
      total_output_tokens: 10_000,
      total_cost_usd: 2.5,
      task_count: 5,
      merged_count: 3,
      merge_rate: 0.6,
      verified_merged_count: 3,
      verified_merge_rate: 0.6,
      priced_task_count: 5,
      unpriced_task_count: 0,
      unpriced_coverage_fraction: 1,
      fallback_task_count: 0,
      fallback_cost_usd: 0,
    },
    unpriced_combos: [],
    approximate_priced_combos: [],
    ...overrides,
  };
}

describe('LlmCostBreakdownPanel', () => {
  beforeEach(() => {
    fetchLlmCostBreakdownMock.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders nothing when no project is selected', () => {
    const { container } = render(<LlmCostBreakdownPanel selectedProjectId={null} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing before data arrives for the selected project', async () => {
    fetchLlmCostBreakdownMock.mockResolvedValue(makeResponse());
    const { container } = render(<LlmCostBreakdownPanel selectedProjectId="proj-1" />);
    // Before the async poller resolves, nothing should be rendered
    expect(container.firstChild).toBeNull();
    await act(async () => {});
  });

  it('renders model rows on successful load', async () => {
    fetchLlmCostBreakdownMock.mockResolvedValue(makeResponse());
    render(<LlmCostBreakdownPanel selectedProjectId="proj-1" />);
    await act(async () => {});
    expect(screen.getByText('LLM Cost by Model')).toBeInTheDocument();
    // Panel renders both mobile card and desktop table layouts — use getAllByText
    expect(screen.getAllByText('claude-sonnet-4-6').length).toBeGreaterThan(0);
  });

  it('shows stale banner when poll fails after initial success', async () => {
    fetchLlmCostBreakdownMock.mockResolvedValueOnce(makeResponse());
    fetchLlmCostBreakdownMock.mockRejectedValue(new Error('network error'));

    vi.useFakeTimers();
    render(<LlmCostBreakdownPanel selectedProjectId="proj-1" />);

    // Initial successful load — no stale banner
    await act(async () => {});
    expect(screen.getAllByText('claude-sonnet-4-6').length).toBeGreaterThan(0);
    expect(screen.queryByText(/stale/i)).toBeNull();

    // Next poll fails
    await act(async () => { vi.advanceTimersByTime(30_000); });
    await act(async () => {});

    // Stale banner visible; data still rendered
    expect(screen.getByText(/stale/i)).toBeInTheDocument();
    expect(screen.getAllByText('claude-sonnet-4-6').length).toBeGreaterThan(0);
  });

  it('renders error placeholder when poll fails before data for the current project', async () => {
    fetchLlmCostBreakdownMock.mockRejectedValue(new Error('network error'));
    render(<LlmCostBreakdownPanel selectedProjectId="proj-1" />);
    await act(async () => {});
    expect(screen.getByText(/unavailable/i)).toBeInTheDocument();
  });

  it('resets error state on project switch', async () => {
    // First project fails; second succeeds
    fetchLlmCostBreakdownMock
      .mockRejectedValueOnce(new Error('network error'))
      .mockResolvedValue(makeResponse());

    const { rerender } = render(<LlmCostBreakdownPanel selectedProjectId="proj-1" />);
    await act(async () => {});
    expect(screen.getByText(/unavailable/i)).toBeInTheDocument();

    rerender(<LlmCostBreakdownPanel selectedProjectId="proj-2" />);
    await act(async () => {});
    // proj-2 data loads successfully — no error, shows model rows
    expect(screen.queryByText(/unavailable/i)).toBeNull();
    expect(screen.getAllByText('claude-sonnet-4-6').length).toBeGreaterThan(0);
  });
});
