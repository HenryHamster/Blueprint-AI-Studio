import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { DirectionProgressSection, type DirectionProgress } from './DirectionProgressSection';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';

// DirectionProgressSection has no API imports — no mocking needed.

const SIMPLE_DIRECTION: DirectionProgress = {
  direction_id: 'dir-1',
  direction_text: 'Improve test coverage for authentication module',
  priority: 'high',
  cycles_in_window: 3,
  tasks_created: 5,
  prs_merged: 3,
  possibly_saturated: false,
};

const RICH_DIRECTION: DirectionProgress = {
  direction_id: 'dir-rich',
  direction_text: 'Refactor backend services to reduce coupling between modules',
  priority: 'medium',
  cycles_in_window: 7,
  tasks_created: 12,
  prs_merged: 8,
  prs_rejected: 2,
  possibly_saturated: false,
  possibly_complete: false,
  acceptance_rate: 0.8,
  first_attempt_success_rate: 0.65,
  quality_score: 0.72,
  cost_per_merged_pr: 3.45,
  cost_per_merge_exceeds_threshold: false,
  estimated_cost_usd: 27.6,
  cost_budget_usd: 50.0,
  budget_utilization_pct: 55.2,
  budget_exceeded: false,
  avg_time_to_merge_hours: 2.3,
  latest_research_at: new Date(Date.now() - 3_600_000).toISOString(),
  latest_insights: ['Coverage improved in auth module', 'Identified coupling hotspot in services layer'],
  recent_finding_titles: ['Add unit tests for token refresh logic', 'Fix missing error handling in middleware'],
  recent_failure_titles: ['Scope overflow in payment module refactor'],
  scope_breakdown: {
    backend: { created: 8, merged: 6, failed: 2 },
    frontend: { created: 4, merged: 2, failed: 1 },
  },
  type_breakdown: {
    fix: { created: 5, merged: 4, failed: 1 },
    feature: { created: 4, merged: 2, failed: 1 },
    refactor: { created: 3, merged: 2, failed: 0 },
  },
  taxonomy_summary: { correctness: 2, test_coverage: 3, architecture: 1 },
  stage_dropout: { planning: 1, implementation: 2, review: 0 },
  rejection_reasons: [
    { task_id: 'task-abc', title: 'Refactor services layer', rejection_note: 'Scope too broad' },
  ],
  lifetime_cycles: 15,
  lifetime_prs_merged: 20,
  lifetime_prs_rejected: 5,
  lifetime_tasks_failed: 3,
  lifetime_conversion_rate: 0.75,
  review_quality: { reviews_dismissed: 1, reviews_overridden: 0, user_issues_caught: 2 },
  // Generalized cross-type success — exercises the dual-metric badge (collapsed)
  // and the "outcomes:" breakdown row (expanded) in the dense layout.
  generalized_successes: 11,
  generalized_terminal_outcomes: 15,
  generalized_health_rate: 0.733,
  outcome_kind_breakdown: {
    code_merge: { success: 8, neutral: 0, failure: 2 },
    research_quality: { success: 2, neutral: 1, failure: 0 },
    audit_findings: { success: 1, neutral: 0, failure: 0 },
  },
};

describe('DirectionProgressSection — viewport regression', () => {
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

  test('empty directions list renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<DirectionProgressSection directions={[]} />);
      });
    });
  });

  test('single direction collapsed renders without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<DirectionProgressSection directions={[SIMPLE_DIRECTION]} />);
      });
    });
  });

  test('rich direction with all badges collapsed renders without overflow at 375px', async () => {
    // RICH_DIRECTION exercises the full badge row: acceptance_rate, quality_score,
    // cost_per_merged_pr, budget_utilization_pct, avg_time_to_merge_hours, estimated_cost_usd.
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <DirectionProgressSection
            directions={[RICH_DIRECTION]}
            medianCostPerMergedPr={2.5}
          />,
        );
      });
    });
  });

  test('expanded direction renders without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;

      await act(async () => {
        r.render(
          <DirectionProgressSection
            directions={[RICH_DIRECTION]}
            medianCostPerMergedPr={2.5}
          />,
        );
      });

      // Click the direction row to expand it.
      const toggleBtn = host.querySelector('button[aria-expanded]');
      if (toggleBtn) {
        await act(async () => { (toggleBtn as HTMLButtonElement).click(); });
      }
    });
  });

  test('expanded direction text is not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;

      await act(async () => {
        r.render(
          <DirectionProgressSection
            directions={[RICH_DIRECTION]}
            medianCostPerMergedPr={2.5}
          />,
        );
      });

      const toggleBtn = host.querySelector('button[aria-expanded]');
      if (toggleBtn) {
        await act(async () => { (toggleBtn as HTMLButtonElement).click(); });
      }
    });
  });

  test('sort select dropdown does not overflow at 375px', async () => {
    // The <select> for sorting is rendered next to the "Directions" header.
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <DirectionProgressSection
            directions={[SIMPLE_DIRECTION, RICH_DIRECTION]}
          />,
        );
      });
    });
  });

  // Note: expectMinTouchTargetAt is intentionally omitted. The direction toggle buttons
  // use `py-1.5` (~28px height) — a pre-existing dense-UI design noted in source with
  // an eslint-disable comment. Touch-target enforcement is deferred to a dedicated audit.
});
