import { afterEach, beforeEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { SystemStatusCard } from './SystemStatusCard';
import { expectNoOverflowAt } from '../test/viewport';
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
    narrative_summary: 'Test narrative summary for the system status card',
    latest_cycle: {
      cycle_id: 'cycle-1',
      started_at: new Date(Date.now() - 3600000).toISOString(),
      completed_at: new Date().toISOString(),
      outcome: 'completed',
      directions_created: 2,
      directions_retired: 1,
      vision_updated: true,
      summary_text: 'Cycle completed successfully',
      memos_available: 5,
      memos_cited: 2,
    },
    memo_pathway_exits: 1,
    memo_ever_cited: { ever_cited: true, first_cycle_id: 'cycle-1', first_cited_at: new Date().toISOString() },
    recent_cycle_memo_stats: [
      { cycle_id: 'cycle-1', started_at: new Date().toISOString(), memos_available: 5, memos_cited: 2 },
    ],
    ...overrides,
  };
}

describe('SystemStatusCard viewport', () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    document.body.removeChild(container);
  });

  test('renders without overflow at 375px (mobile)', async () => {
    const digest = createMockDigest();
    await expectNoOverflowAt(375, async () => {
      act(() => {
        root.render(<SystemStatusCard digest={digest} loading={false} />);
      });
    });
  });

  test('renders without overflow at 768px (tablet)', async () => {
    const digest = createMockDigest();
    await expectNoOverflowAt(768, async () => {
      act(() => {
        root.render(<SystemStatusCard digest={digest} loading={false} />);
      });
    });
  });

  test('renders without overflow at 1024px (desktop)', async () => {
    const digest = createMockDigest();
    await expectNoOverflowAt(1024, async () => {
      act(() => {
        root.render(<SystemStatusCard digest={digest} loading={false} />);
      });
    });
  });
});
