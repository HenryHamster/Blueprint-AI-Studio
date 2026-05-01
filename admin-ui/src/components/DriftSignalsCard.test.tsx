import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DriftSignalsCard } from './DriftSignalsCard';

const { apiFetchMock } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
}));

vi.mock('../api', () => ({
  apiFetch: (path: string) => apiFetchMock(path),
}));

function jsonResponse(data: unknown): Response {
  return { ok: true, json: async () => data } as Response;
}

function makeSnapshot(overrides: Record<string, unknown> = {}) {
  return {
    flagged_directions: [],
    flagged_count: 0,
    total_directions: 2,
    any_system_flagged: false,
    generated_at: '2026-04-28T00:00:00Z',
    window_days: 7,
    ...overrides,
  };
}

describe('DriftSignalsCard', () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  it('renders nothing when flagged_count is 0', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse(makeSnapshot({ flagged_count: 0 })));

    const { container } = render(<DriftSignalsCard projectId="proj-1" />);

    // Wait for the fetch to resolve
    await act(async () => {});
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing while snapshot is null (no response yet)', () => {
    apiFetchMock.mockImplementation(() => new Promise(() => {}));

    const { container } = render(<DriftSignalsCard projectId="proj-1" />);
    expect(container.firstChild).toBeNull();
  });

  it('shows flagged count badge when directions are flagged', async () => {
    apiFetchMock.mockResolvedValue(
      jsonResponse(
        makeSnapshot({
          flagged_count: 2,
          flagged_directions: [
            {
              direction_id: 'd-1',
              direction_text: 'Ship retry backoff',
              over_claim_flag: true,
              stalled_despite_churn_flag: false,
              claimed_changes_count: 5,
              actually_merged_prs: 0,
              claim_shipped_consistency: 0.0,
            },
            {
              direction_id: 'd-2',
              direction_text: 'Fix flaky CI',
              over_claim_flag: false,
              stalled_despite_churn_flag: true,
              claimed_changes_count: 6,
              actually_merged_prs: 0,
              claim_shipped_consistency: 0.0,
            },
          ],
        }),
      ),
    );

    render(<DriftSignalsCard projectId="proj-1" />);

    await act(async () => {});
    expect(screen.getByText(/Drift Signals — 2 directions flagged/)).toBeInTheDocument();
  });

  it('expands to show direction entries when clicked', async () => {
    apiFetchMock.mockResolvedValue(
      jsonResponse(
        makeSnapshot({
          flagged_count: 1,
          flagged_directions: [
            {
              direction_id: 'd-oc',
              direction_text: 'Improve merge throughput',
              over_claim_flag: true,
              stalled_despite_churn_flag: false,
              claimed_changes_count: 4,
              actually_merged_prs: 0,
              claim_shipped_consistency: 0.0,
            },
          ],
        }),
      ),
    );

    const user = userEvent.setup();
    render(<DriftSignalsCard projectId="proj-1" />);

    await act(async () => {});
    // Before expand, direction text not visible
    expect(screen.queryByText('Improve merge throughput')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button'));
    expect(screen.getByText('Improve merge throughput')).toBeInTheDocument();
    expect(screen.getByText('over-claim')).toBeInTheDocument();
    expect(screen.getByText('0/4 merged')).toBeInTheDocument();
  });

  it('shows stalled badge when stalled_despite_churn_flag is set', async () => {
    apiFetchMock.mockResolvedValue(
      jsonResponse(
        makeSnapshot({
          flagged_count: 1,
          flagged_directions: [
            {
              direction_id: 'd-stall',
              direction_text: 'Clear tech debt backlog',
              over_claim_flag: false,
              stalled_despite_churn_flag: true,
              claimed_changes_count: 7,
              actually_merged_prs: 0,
              claim_shipped_consistency: 0.0,
            },
          ],
        }),
      ),
    );

    const user = userEvent.setup();
    render(<DriftSignalsCard projectId="proj-1" />);

    await act(async () => {});
    await user.click(screen.getByRole('button'));
    expect(screen.getByText('stalled')).toBeInTheDocument();
    expect(screen.queryByText('over-claim')).not.toBeInTheDocument();
  });

  it('shows a consistency badge and collapsed flagged-system summary', async () => {
    apiFetchMock.mockResolvedValue(
      jsonResponse(
        makeSnapshot({
          flagged_count: 1,
          any_system_flagged: true,
          by_system: {
            'sys-alpha': {
              over_claim_flag: true,
              stalled_despite_churn_flag: false,
              claimed_changes_count: 5,
              actually_merged_prs: 1,
              claim_shipped_consistency: 0.2,
            },
            'sys-beta': {
              over_claim_flag: false,
              stalled_despite_churn_flag: false,
              claimed_changes_count: 1,
              actually_merged_prs: 1,
              claim_shipped_consistency: 1,
            },
          },
          flagged_directions: [
            {
              direction_id: 'd-1',
              direction_text: 'Stabilize rollout ownership',
              over_claim_flag: true,
              stalled_despite_churn_flag: false,
              claimed_changes_count: 4,
              actually_merged_prs: 1,
              claim_shipped_consistency: 0.25,
            },
          ],
        }),
      ),
    );

    const user = userEvent.setup();
    render(<DriftSignalsCard projectId="proj-1" />);

    await act(async () => {});
    await user.click(screen.getByRole('button', { name: /Drift Signals/i }));

    expect(screen.getByText('25% consistent')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /System drift summary/i })).toBeInTheDocument();
    expect(screen.queryByText('sys-alpha')).not.toBeInTheDocument();
    expect(screen.queryByText('sys-beta')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /System drift summary/i }));
    expect(screen.getByText('sys-alpha')).toBeInTheDocument();
    expect(screen.getByText('20% consistent')).toBeInTheDocument();
    expect(screen.queryByText('sys-beta')).not.toBeInTheDocument();
  });

  it('gracefully skips newer drift fields when older responses omit them', async () => {
    apiFetchMock.mockResolvedValue(
      jsonResponse({
        flagged_count: 1,
        flagged_directions: [
          {
            direction_id: 'd-legacy',
            direction_text: 'Legacy payload path',
            over_claim_flag: true,
            stalled_despite_churn_flag: false,
            claimed_changes_count: 3,
            actually_merged_prs: 0,
          },
        ],
        total_directions: 1,
        generated_at: '2026-04-28T00:00:00Z',
        window_days: 7,
      }),
    );

    const user = userEvent.setup();
    render(<DriftSignalsCard projectId="proj-1" />);

    await act(async () => {});
    await user.click(screen.getByRole('button', { name: /Drift Signals/i }));

    expect(screen.getByText('Legacy payload path')).toBeInTheDocument();
    expect(screen.getByText('0/3 merged')).toBeInTheDocument();
    expect(screen.queryByText(/consistent/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /System drift summary/i })).not.toBeInTheDocument();
  });

  it('fetches with the correct project_id query param', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse(makeSnapshot()));

    render(<DriftSignalsCard projectId="my-project-xyz" />);
    await act(async () => {});

    expect(apiFetchMock).toHaveBeenCalledWith(
      expect.stringContaining('project_id=my-project-xyz'),
    );
  });

  it('renders sparkline when drift_history has sufficient points', async () => {
    const driftHistory = [
      { snapshot_at: '2026-04-22T00:00:00Z', claim_shipped_consistency: 0.8, over_claim_flag: false },
      { snapshot_at: '2026-04-23T00:00:00Z', claim_shipped_consistency: 0.5, over_claim_flag: false },
      { snapshot_at: '2026-04-24T00:00:00Z', claim_shipped_consistency: 0.3, over_claim_flag: true },
      { snapshot_at: '2026-04-25T00:00:00Z', claim_shipped_consistency: 0.2, over_claim_flag: true },
      { snapshot_at: '2026-04-26T00:00:00Z', claim_shipped_consistency: 0.1, over_claim_flag: true },
    ];

    apiFetchMock.mockResolvedValue(
      jsonResponse(
        makeSnapshot({
          flagged_count: 1,
          flagged_directions: [
            {
              direction_id: 'd-hist',
              direction_text: 'Ship retry backoff',
              over_claim_flag: true,
              stalled_despite_churn_flag: false,
              claimed_changes_count: 5,
              actually_merged_prs: 0,
              claim_shipped_consistency: 0.1,
              drift_history: driftHistory,
            },
          ],
        }),
      ),
    );

    const user = userEvent.setup();
    render(<DriftSignalsCard projectId="proj-sparkline" />);

    await act(async () => {});
    await user.click(screen.getByRole('button'));

    expect(screen.getByRole('img', { name: /consistency trend/i })).toBeInTheDocument();
  });

  it('omits sparkline when drift_history is absent', async () => {
    apiFetchMock.mockResolvedValue(
      jsonResponse(
        makeSnapshot({
          flagged_count: 1,
          flagged_directions: [
            {
              direction_id: 'd-no-hist',
              direction_text: 'No history direction',
              over_claim_flag: true,
              stalled_despite_churn_flag: false,
              claimed_changes_count: 3,
              actually_merged_prs: 0,
              claim_shipped_consistency: 0.0,
            },
          ],
        }),
      ),
    );

    const user = userEvent.setup();
    render(<DriftSignalsCard projectId="proj-no-spark" />);

    await act(async () => {});
    await user.click(screen.getByRole('button'));

    expect(screen.queryByRole('img', { name: /consistency trend/i })).not.toBeInTheDocument();
  });
});
