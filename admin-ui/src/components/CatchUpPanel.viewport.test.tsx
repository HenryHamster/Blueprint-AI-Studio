import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { CatchUpPanel } from './CatchUpPanel';
import { expectNoOverflowAt } from '../test/viewport';

// Mock the api module so CatchUpPanel doesn't try to fetch real data
vi.mock('../api', () => ({
  apiFetch: vi.fn(async (url: string) => {
    if (url.includes('/digest')) {
      return new Response(
        JSON.stringify({
          window_hours: 24,
          generated_at: '2026-05-06T00:00:00Z',
          completed_count: 5,
          failed_count: 1,
          pending_approval_count: 0,
          research_cycles: 1,
          prs_opened: 3,
          active_count: 0,
          running_count: 0,
          needs_attention: [
            {
              task_id: 'task-fail-1',
              title: 'fix: resolve viewport overflow issue on dashboard',
              reason: 'failed',
              error_snippet: 'Layout constraint violation',
              pr_url: null,
              failure_class: 'CODE_ERROR',
            },
          ],
          recent_failures: [
            {
              task_id: 'task-fail-2',
              title: 'feat: add analytics tracking module',
              failed_at: '2026-05-05T12:30:00Z',
              error_snippet: 'Module import failed',
              failure_class: 'IMPORT_ERROR',
            },
          ],
          failure_groups: [
            {
              failure_class: 'CODE_ERROR',
              label: 'Code Error',
              count: 1,
              tasks: [],
            },
          ],
          recent_completions: [
            {
              task_id: 'task-complete-1',
              title: 'feat: mobile-responsive layout for core pages',
              completed_at: '2026-05-06T00:00:00Z',
              pr_url: 'https://github.com/example/repo/pull/1',
              task_type: 'create',
              direction_id: null,
              direction_text: null,
              duration_seconds: 3600,
            },
          ],
          direction_progress: [
            {
              id: 'd-001',
              text: 'Improve admin UI mobile responsiveness',
              priority: 'high',
              findings: 5,
              latest_status: 'in-progress',
              matching_tasks: 3,
              task_completion_rate: 0.67,
              current_effort_points: 8,
              target_effort_points: 13,
            },
          ],
          active_tasks: [],
          fresh_reviewer_effectiveness: null,
        }),
        { status: 200 },
      );
    }
    return new Response('{}', { status: 200 });
  }),
  API_BASE_URL: 'http://localhost:8000',
}));

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
});
