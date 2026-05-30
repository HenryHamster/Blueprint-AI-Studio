import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SystemStatusCard } from './SystemStatusCard';
import type { DigestData } from './DashboardHome';

function createMockDigest(overrides: Partial<DigestData> = {}): DigestData {
  return {
    failed_count: 0,
    pending_approval_count: 0,
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
});
