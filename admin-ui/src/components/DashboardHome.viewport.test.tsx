import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { DashboardHome } from './DashboardHome';
import { expectNoOverflowAt } from '../test/viewport';

// DashboardHome (and its sub-components) transitively import apiFetch from
// '../api', which pulls in Firebase. Mock the module so the test does not
// require real auth and so we can control the digest payload.
vi.mock('../api', () => ({
  apiFetch: vi.fn(async (url: string) => {
    if (url.includes('/digest')) {
      return new Response(
        JSON.stringify({
          failed_count: 3,
          pending_approval_count: 1,
          direction_progress: [],
          recent_completions: [
            {
              task_id: 'task-abc',
              title: 'feat: add mobile-responsive grid layout to DashboardHome operator page',
              completed_at: '2026-05-06T00:00:00Z',
              pr_url: 'https://github.com/example/repo/pull/42',
              task_type: 'create',
              direction_id: null,
              direction_text: null,
              duration_seconds: 3600,
            },
          ],
          failure_groups: [
            { failure_class: 'CODE_ERROR', label: 'Code Error', count: 2, tasks: [] },
            { failure_class: 'AGENT_TIMEOUT', label: 'Timeout', count: 1, tasks: [] },
          ],
          success_rate: 0.82,
          trend: 'stable',
          trend_history: [],
          completed_count: 24,
          pipeline_conversion_rate: 0.61,
          pipeline_acceptance_rate: 0.74,
          total_estimated_cost_usd: 8.42,
          failure_taxonomy_distribution: {
            categories: {
              code_defect: { count: 2, percentage: 67 },
              scope_related: { count: 1, percentage: 33 },
            },
            total_failures: 3,
            dominant_category: 'code_defect',
          },
        }),
        { status: 200 },
      );
    }
    // All other endpoints return empty/ok so sub-components enter their
    // idle/empty states without throwing.
    return new Response('{}', { status: 200 });
  }),
}));

describe('DashboardHome — viewport regression', () => {
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

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      // Match the app-shell padding applied at mobile (p-3 = 12px each side);
      // narrower than the viewport so we approximate real operator conditions.
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <DashboardHome
            tasks={[]}
            selectedProjectId={null}
            onViewTask={() => {}}
          />,
        );
      });
      // Allow async state updates (mock fetches resolve) to settle.
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    });
  });
});
