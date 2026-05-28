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

  test('failure groups section with all badge types does not overflow at 375px', async () => {
    vi.mocked(apiFetch).mockImplementationOnce(async (url: string) => {
      if (url.includes('/digest')) {
        return new Response(JSON.stringify({
          window_hours: 24,
          generated_at: '2026-05-06T00:00:00Z',
          completed_count: 3,
          failed_count: 5,
          pending_approval_count: 0,
          research_cycles: 0,
          prs_opened: 2,
          active_count: 0,
          running_count: 0,
          needs_attention: [],
          recent_failures: [],
          failure_groups: [
            {
              failure_class: 'AGENT_TIMEOUT',
              label: 'Agent Timeout',
              count: 1,
              tasks: [
                {
                  task_id: 'fg-t1',
                  title: 'refactor: extract and decompose the recent-failures classification section from CatchUpPanel including all badge variants',
                  error_snippet: 'TimeoutError: Agent exceeded wall-clock limit\n  at runAgent (index.ts:2042)',
                  attempt_count: 3,
                  waiting_hours: 4,
                  taxonomy: 'scope_related',
                  timeout_subtype: 'execution_timeout',
                  failure_stage: 'test',
                  direction_id: 'd-001',
                  direction_text: 'Decompose large admin-ui panels into smaller single-responsibility components with focused test coverage',
                },
              ],
            },
            {
              failure_class: 'CODE_ERROR',
              label: 'Code Error',
              count: 1,
              tasks: [
                {
                  task_id: 'fg-t2',
                  title: 'fix: handle duplicate escalation reason labels in pipeline regression dispatcher retry loops',
                  error_snippet: null,
                  attempt_count: 1,
                  waiting_hours: 1,
                  taxonomy: 'code_defect',
                  timeout_subtype: null,
                  failure_stage: 'lint',
                  direction_id: null,
                  direction_text: null,
                },
              ],
            },
          ],
          recent_completions: [],
          direction_progress: [],
          active_tasks: [],
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
      // Expand the "Recent failures" section
      const failuresToggle = Array.from(host!.querySelectorAll('button'))
        .find(b => b.textContent?.includes('Recent failures'));
      if (failuresToggle) {
        await act(async () => { failuresToggle.click(); });
      }
      // Expand the first failure group to show task rows with all badges
      const groupToggle = host!.querySelector<HTMLButtonElement>('button[aria-expanded="false"]');
      if (groupToggle) {
        await act(async () => { groupToggle.click(); });
      }
    }, { waitMs: 50 });
  });

  test('review quality section expanded does not overflow at 375px', async () => {
    vi.mocked(apiFetch).mockImplementationOnce(async (url: string) => {
      if (url.includes('/digest')) {
        return new Response(JSON.stringify({
          window_hours: 24, generated_at: '2026-05-06T00:00:00Z',
          completed_count: 0, failed_count: 0, pending_approval_count: 0,
          research_cycles: 0, prs_opened: 0, active_count: 0, running_count: 0,
          needs_attention: [], recent_failures: [], failure_groups: [],
          recent_completions: [], direction_progress: [], active_tasks: [],
          self_review_effectiveness: {
            with_self_review_approval_rate: 0.85, without_self_review_approval_rate: 0.6,
            clean_verdict_approval_rate: null, fixed_verdict_approval_rate: null, sample_size: 25,
          },
          self_review_prediction_accuracy: {
            prediction_hit_rate: 0.78, miss_rate: 0.15, false_alarm_rate: 0.07, sample_size: 40,
          },
          self_review_skip_rate: {
            overall_rate: 0.08, skipped_count: 4, sample_size: 50,
            by_task_type: { create: { rate: 0.1, skipped_count: 2, sample_size: 20 } },
          },
          review_accuracy: {
            total_reviews: 30, overrides: 2, dismissals: 1, misses: 3,
            total_approvals: 27, precision: 0.87, recall: 0.9,
          },
          first_review_approval_rate: { overall: 0.75, sample_size: 20 },
          fresh_reviewer_effectiveness: {
            fresh_reviewer_sample_size: 10, fresh_reviewer_first_attempt_approval_rate: 0.8,
            baseline_first_attempt_approval_rate: 0.7,
            per_category_hit_rate: { correctness: 0.85 },
            reviewer_finding_added_value: {}, prediction_sample_size: 10,
          },
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
      const toggle = Array.from(host!.querySelectorAll('button'))
        .find(b => b.textContent?.includes('Review quality'));
      if (toggle) {
        await act(async () => { toggle.click(); });
      }
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
