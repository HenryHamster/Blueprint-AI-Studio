import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SystemStatusCard } from './SystemStatusCard';
import type { DigestData } from './DashboardHome';

function createMockDigest(overrides: Partial<DigestData> = {}): DigestData {
  return {
    failed_count: 0,
    pending_approval_count: 0,
    completed_count: 0,
    active_count: 0,
    running_count: 0,
    starting_count: 0,
    evaluating_count: 0,
    queued_count: 0,
    research_cycles: 0,
    prs_opened: 0,
    direction_progress: [],
    recent_completions: [],
    failure_groups: [],
    narrative_summary: 'Test narrative',
    latest_cycle: {
      cycle_id: 'cycle-1',
      started_at: new Date(Date.now() - 3600000).toISOString(),
      completed_at: new Date().toISOString(),
      outcome: 'completed',
      directions_created: 2,
      directions_retired: 1,
      vision_updated: true,
      summary_text: 'Test summary',
      memos_available: 5,
      memos_cited: 2,
    },
    memo_pathway_exits: 1,
    memo_ever_cited: { ever_cited: true, first_cycle_id: 'cycle-1', first_cited_at: new Date().toISOString() },
    recent_cycle_memo_stats: [{ cycle_id: 'cycle-1', started_at: new Date().toISOString(), memos_available: 5, memos_cited: 2 }],
    ...overrides,
  };
}

describe('SystemStatusCard', () => {
  it('renders loading skeleton when loading is true', () => {
    render(<SystemStatusCard digest={null} loading={true} />);
    expect(screen.getByRole('heading', { name: /system status/i })).toBeInTheDocument();
  });

  it('renders no content message when digest is null and loading is false', () => {
    render(<SystemStatusCard digest={null} loading={false} />);
    expect(screen.getByText('No recent activity')).toBeInTheDocument();
  });

  it('renders narrative summary when present', () => {
    const digest = createMockDigest({ narrative_summary: 'Test narrative summary' });
    render(<SystemStatusCard digest={digest} loading={false} />);
    expect(screen.getByText('Test narrative summary')).toBeInTheDocument();
  });

  it('renders latest cycle info', () => {
    const digest = createMockDigest();
    render(<SystemStatusCard digest={digest} loading={false} />);
    expect(screen.getByText(/2 created, 1 retired/)).toBeInTheDocument();
  });

  it('renders memo pathway stats', () => {
    const digest = createMockDigest({ memo_pathway_exits: 3 });
    render(<SystemStatusCard digest={digest} loading={false} />);
    expect(screen.getByText(/3 exits/)).toBeInTheDocument();
  });

  it('surfaces the review/fix convergence verdict as its own line when signal exists', () => {
    const digest = createMockDigest({
      narrative_summary:
        'Pipeline is healthy. Review/fix churn is falling, with stale-review-context the dominant blocker on 4 non-converged chains. Two directions advanced.',
    });
    render(<SystemStatusCard digest={digest} loading={false} />);
    const verdict = screen.getByLabelText('Review/fix convergence verdict');
    expect(verdict).toHaveTextContent(
      'Review/fix churn is falling, with stale-review-context the dominant blocker on 4 non-converged chains.',
    );
    // The verdict sentence is lifted out of the surrounding prose paragraph.
    expect(screen.getByText(/Pipeline is healthy\. Two directions advanced\./)).toBeInTheDocument();
  });

  it('suppresses the convergence verdict line on thin windows (no churn sentence)', () => {
    const digest = createMockDigest({
      narrative_summary: 'Pipeline is healthy. Two directions advanced.',
    });
    render(<SystemStatusCard digest={digest} loading={false} />);
    expect(screen.queryByLabelText('Review/fix convergence verdict')).not.toBeInTheDocument();
    expect(screen.getByText('Pipeline is healthy. Two directions advanced.')).toBeInTheDocument();
  });

  it('renders the strategic challenge chip on the latest-cycle line when present', () => {
    const base = createMockDigest().latest_cycle ?? { started_at: new Date().toISOString() };
    const digest = createMockDigest({
      latest_cycle: {
        ...base,
        strategic_judgment_challenge: {
          mode: 'blocked',
          verdict: 'blocked',
          warned_count: 0,
          blocked_count: 2,
        },
      },
    });
    render(<SystemStatusCard digest={digest} loading={false} />);
    const chip = screen.getByLabelText(/strategic judgment challenge/i);
    expect(chip).toHaveTextContent('challenge: blocked · blocked');
    expect(chip).toHaveTextContent('2 blocked');
  });

  it('renders no challenge chip when the latest cycle carries no summary', () => {
    const digest = createMockDigest();
    render(<SystemStatusCard digest={digest} loading={false} />);
    expect(screen.queryByLabelText(/strategic judgment challenge/i)).not.toBeInTheDocument();
  });
});
