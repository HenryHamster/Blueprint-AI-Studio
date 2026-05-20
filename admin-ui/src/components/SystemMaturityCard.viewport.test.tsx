import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { SystemMaturityCard, type SystemMaturityRow } from './SystemMaturityCard';
import { expectNoOverflowAt } from '../test/viewport';

const rows: SystemMaturityRow[] = [
  {
    system_id: 'autonomous-research-pipeline-with-very-long-identifier-that-could-overflow',
    score: 82,
    recommended_mode: 'ship',
    top_issues: [
      { description: 'Investigation conversion below threshold for INFRA_RELATED triggers' },
      { description: 'Missing observability on residual remediation funnel for sustained signals' },
    ],
    last_audited_at: '2026-05-06T10:00:00Z',
  },
  {
    system_id: 'orchestrator-task-lifecycle',
    score: 47,
    recommended_mode: 'fundamentals',
    top_issues: [
      { description: 'TASK_LIFECYCLE_DEFERRAL_LOOP needs convergence proof under high concurrency' },
    ],
    last_audited_at: '2026-05-05T22:00:00Z',
  },
  {
    system_id: 'fix-loop-convergence-tracker',
    score: 28,
    recommended_mode: 'audit',
    top_issues: [],
    last_audited_at: '2026-05-04T08:00:00Z',
  },
  {
    system_id: 'paused-direction-self-recovery',
    score: 15,
    recommended_mode: 'pause',
    top_issues: [
      { description: 'No alerts on auto-pause hold expiry' },
    ],
    last_audited_at: '2026-05-03T14:00:00Z',
  },
];

describe('SystemMaturityCard — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('renders rows without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<SystemMaturityCard rows={rows} loading={false} />);
      });
    });
  });

  test('renders rows without horizontal overflow at 768px', async () => {
    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<SystemMaturityCard rows={rows} loading={false} />);
      });
    });
  });

  test('renders empty state without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<SystemMaturityCard rows={[]} loading={false} />);
      });
    });
  });
});
