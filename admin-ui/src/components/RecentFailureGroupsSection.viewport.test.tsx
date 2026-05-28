import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { RecentFailureGroupsSection } from './RecentFailureGroupsSection';
import { expectNoOverflowAt } from '../test/viewport';

describe('RecentFailureGroupsSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('renders failure groups expanded with long snippets without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <RecentFailureGroupsSection
            extraFailureGroups={[
              {
                failure_class: 'lint_baseline_violation',
                label: 'Lint baseline violation in shared module on narrow-viewport refactor cluster',
                count: 8,
                tasks: [
                  {
                    task_id: 'task-aaaa-bbbb-cccc-dddd-eeee-1',
                    title: 'refactor: extract OperatorReviewQualityDashboardSection from CatchUpPanel cluster',
                    error_snippet: 'AssertionError: viewport overflow at 375px — child element CatchUpReviewQualitySection extended host width by 28px due to nested non-shrinking flex content in dense metrics grid layout',
                    attempt_count: 3,
                    waiting_hours: 4.2,
                    taxonomy: 'frontend_lint',
                    failure_stage: 'ci_lint',
                    direction_id: 'd-001',
                    direction_text: 'Improve admin UI mobile responsiveness across all operator-facing panels for narrow widths',
                  },
                  {
                    task_id: 'task-aaaa-bbbb-cccc-dddd-eeee-2',
                    title: 'fix: address viewport overflow surfaced on extracted RecurringPatternsSection at 375px',
                    error_snippet: 'eslint @typescript-eslint/no-non-null-assertion (warn) — pre-existing baseline includes 4 forbidden-non-null-assertion warnings in MessagesPanel; refactor extraction copied them into new component',
                    attempt_count: 2,
                    failure_stage: 'ci_frontend',
                  },
                ],
              },
              {
                failure_class: 'viewport_overflow',
                label: 'Viewport overflow at 375px',
                count: 4,
                tasks: [
                  {
                    task_id: 'task-aaaa-bbbb-cccc-dddd-eeee-3',
                    title: 'refactor: extract MessagesSummaryCard from MessagesPanel',
                    error_snippet: 'expected operator dashboard section to render without horizontal overflow at 375px but child width was 392px',
                    attempt_count: 5,
                    waiting_hours: 12.5,
                    taxonomy: 'viewport_regression',
                    timeout_subtype: null,
                    failure_stage: 'ci_viewport',
                  },
                ],
              },
            ]}
            activeTaxonomyFilter={null}
            activeStageFilter={null}
            trendHistory={[
              { period_end: '2026-05-21', success_rate: 0.84, completed: 412, failed: 78, first_attempt_success_rate: 0.71, period_cost_usd: 38.42 },
              { period_end: '2026-05-28', success_rate: 0.79, completed: 388, failed: 102, first_attempt_success_rate: 0.66, period_cost_usd: 41.18 },
            ]}
            costByFailureClass={{
              lint_baseline_violation: 12.42,
              viewport_overflow: 6.81,
            }}
            expandedSnippets={new Set(['task-aaaa-bbbb-cccc-dddd-eeee-1', 'task-aaaa-bbbb-cccc-dddd-eeee-3'])}
            onToggleSnippet={() => { /* no-op */ }}
            onSelectTask={() => { /* no-op */ }}
            failuresExpanded
            onToggleFailuresExpanded={() => { /* no-op */ }}
          />,
        );
      });
    });
  });
});
