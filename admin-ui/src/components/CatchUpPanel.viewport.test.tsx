import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { CatchUpPanel } from './CatchUpPanel';
import { expectNoOverflowAt, expectNoTextTruncationAt, expectMinTouchTargetAt, stressDigestPayload } from '../test/viewport';
import { apiFetch } from '../api';

// Mock the api module so CatchUpPanel doesn't try to fetch real data.
// Use a partial mock (vi.importActual) so transitively-imported helpers
// like `authGet` (used by TaskCard.tsx, which CatchUpPanel imports) keep
// their real exports — otherwise the static factory replaces the whole
// module surface and ESM import resolution fails at load time.
vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api');
  return {
    ...actual,
    apiFetch: vi.fn(async (url: string) => {
    if (url.includes('/digest')) {
      return new Response(JSON.stringify(stressDigestPayload()), { status: 200 });
    }
    return new Response('{}', { status: 200 });
  }),
    API_BASE_URL: 'http://localhost:8000',
  };
});

describe('CatchUpPanel — viewport regression', () => {
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

  test('task titles, error snippets, and direction text are not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <CatchUpPanel
            selectedProjectId="project-123"
            lastVisitTimestamp={Date.now() - 24 * 60 * 60 * 1000}
            refreshInterval={60000}
            onSelectTask={() => {}}
            onOpenActivityFeed={() => {}}
            needsAttentionCount={2}
            onApprove={() => {}}
            onRetry={() => {}}
            onBulkApprove={() => {}}
            onBulkRetry={() => {}}
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    }, { waitMs: 50 });
  });

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <CatchUpPanel
            selectedProjectId="project-123"
            lastVisitTimestamp={Date.now() - 24 * 60 * 60 * 1000}
            refreshInterval={60000}
            onSelectTask={() => {}}
            onOpenActivityFeed={() => {}}
            needsAttentionCount={1}
            onApprove={() => {}}
            onRetry={() => {}}
            onBulkApprove={() => {}}
            onBulkRetry={() => {}}
          />,
        );
      });
      // Allow async state updates (mock fetches resolve) to settle
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    });
  });

  test('active task rows with all badges do not overflow 375px', async () => {
    vi.mocked(apiFetch).mockImplementationOnce(async (url: string) => {
      if (url.includes('/digest')) {
        return new Response(JSON.stringify({
          window_hours: 24,
          generated_at: '2026-05-06T00:00:00Z',
          completed_count: 0,
          failed_count: 0,
          pending_approval_count: 0,
          research_cycles: 0,
          prs_opened: 0,
          active_count: 2,
          running_count: 2,
          needs_attention: [],
          recent_failures: [],
          failure_groups: [],
          recent_completions: [],
          direction_progress: [],
          active_tasks: [
            {
              task_id: 'task-active-1',
              title: 'feat(auth): implement token-refresh flow with sliding expiry and cross-tab sync for long-running operator sessions',
              status: 'running',
              task_type: 'create',
              started_at: '2026-05-06T00:00:00Z',
              running_cost_usd: 1.23,
              direction_text: 'Improve admin UI mobile responsiveness across all operator-facing panels including kanban and task detail',
              scope: 'medium',
              review_iteration: 2,
              review_cap: 3,
            },
            {
              task_id: 'task-active-2',
              title: 'fix(backend): handle duplicate escalation_reason labels in pipeline_regression_dispatcher retry loops',
              status: 'running',
              task_type: 'fix',
              started_at: '2026-05-06T00:00:00Z',
              running_cost_usd: 0.87,
              direction_text: 'Test infrastructure and coverage improvements for edge cases across pipeline modules',
              scope: 'small',
              review_iteration: 1,
              review_cap: 3,
            },
          ],
          fresh_reviewer_effectiveness: null,
        }), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    });

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <CatchUpPanel
            selectedProjectId="project-123"
            lastVisitTimestamp={Date.now() - 24 * 60 * 60 * 1000}
            refreshInterval={60000}
            onSelectTask={() => {}}
            onOpenActivityFeed={() => {}}
            needsAttentionCount={0}
            onApprove={() => {}}
            onRetry={() => {}}
            onBulkApprove={() => {}}
            onBulkRetry={() => {}}
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    }, { waitMs: 50 });
  });

  test('primary triage actions (Approve/Retry buttons) meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <CatchUpPanel
            selectedProjectId="project-123"
            lastVisitTimestamp={Date.now() - 24 * 60 * 60 * 1000}
            refreshInterval={60000}
            onSelectTask={() => {}}
            onOpenActivityFeed={() => {}}
            needsAttentionCount={2}
            onApprove={() => {}}
            onRetry={() => {}}
            onBulkApprove={() => {}}
            onBulkRetry={() => {}}
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    }, { selector: 'button:is(.bg-emerald-500\\/10, .bg-blue-500\\/10)', waitMs: 50 });
  });
});
