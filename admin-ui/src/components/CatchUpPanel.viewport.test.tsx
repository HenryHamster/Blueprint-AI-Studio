import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { CatchUpPanel } from './CatchUpPanel';
import { expectNoOverflowAt, expectNoTextTruncationAt, expectMinTouchTargetAt } from '../test/viewport';
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
              direction_id: 'd-001',
              direction_text: 'Improve admin UI mobile responsiveness',
              priority: 'high',
              cycles_in_window: 3,
              tasks_created: 8,
              prs_merged: 5,
              possibly_saturated: false,
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
    vi.mocked(apiFetch).mockImplementationOnce(async (url: string) => {
      if (url.includes('/digest')) {
        return new Response(
          JSON.stringify({
            window_hours: 24,
            generated_at: '2026-05-06T00:00:00Z',
            completed_count: 8,
            failed_count: 3,
            pending_approval_count: 2,
            research_cycles: 2,
            prs_opened: 5,
            active_count: 1,
            running_count: 1,
            needs_attention: [
              {
                task_id: 'task-fail-1',
                title: 'feat(auth): implement token-refresh flow with sliding expiry and cross-tab sync for long-running operator sessions',
                reason: 'failed',
                error_snippet: 'backend/testing/test_pipeline_regression_dispatcher.py::TestDispatcher::test_emit_duplicate_escalation_reason_on_retry FAILED',
                pr_url: null,
                failure_class: 'CODE_ERROR',
              },
              {
                task_id: 'task-fail-2',
                title: 'fix(backend): handle edge case where pipeline_regression_dispatcher emits duplicate escalation_reason labels on retry loops',
                reason: 'failed',
                error_snippet: 'AssertionError: Expected labels to be unique but found [\'escalated_cross_layer\', \'escalated_cross_layer\'] — dedup guard not applied in dispatch_single_signal',
                pr_url: 'https://github.com/HenryHamster/BlueprintAIStudio/pull/4923',
                failure_class: 'TEST_FAILURE',
                scope: 'small',
                direction_text: 'Improve test infrastructure and coverage for edge cases',
                merge_retries_saved: 3,
                waiting_hours: 2,
              },
            ],
            recent_failures: [
              {
                task_id: 'task-fail-3',
                title: 'refactor: extract shared viewport-assertion helpers from per-component test files into admin-ui/src/test/viewport.ts',
                failed_at: '2026-05-05T12:30:00Z',
                error_snippet: 'Module import failed: admin-ui/src/test/viewport.ts not found at resolution path',
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
                title: 'feat: add mobile-responsive layout for core operator-facing panels including kanban board and task detail drawer',
                completed_at: '2026-05-06T00:00:00Z',
                pr_url: 'https://github.com/example/repo/pull/4600',
                task_type: 'create',
                direction_id: 'd-001',
                direction_text: 'Improve admin UI mobile responsiveness',
                duration_seconds: 3600,
              },
            ],
            direction_progress: [
              {
                direction_id: 'd-001',
                direction_text: 'Improve admin UI mobile responsiveness across all operator-facing panels including kanban board, task detail drawer, and metrics dashboard',
                priority: 'high',
                cycles_in_window: 6,
                tasks_created: 14,
                prs_merged: 8,
                possibly_saturated: false,
              },
            ],
            active_tasks: [],
            fresh_reviewer_effectiveness: null,
          }),
          { status: 200 },
        );
      }
      return new Response('{}', { status: 200 });
    });

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
