import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { CatchUpCostDiagnosticsSection } from './CatchUpCostDiagnosticsSection';
import { expectNoOverflowAt } from '../test/viewport';

describe('CatchUpCostDiagnosticsSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('renders dense cost-diagnostics content without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <CatchUpCostDiagnosticsSection
            failureTaxonomyDistribution={{
              categories: {
                scope_related: { count: 14, percentage: 40 },
                review_quality: { count: 10, percentage: 29 },
                infra_related: { count: 7, percentage: 20 },
                code_defect: { count: 4, percentage: 11 },
              },
              total_failures: 35,
              dominant_category: 'scope_related',
            }}
            costByFailureClass={{
              'scope_mismatch_class': 12.50,
              'review_miss_class': 8.20,
            }}
            failureGroups={[
              { failure_class: 'scope_mismatch_class', tasks: [{ taxonomy: 'scope_related' }, { taxonomy: 'scope_related' }] },
              { failure_class: 'review_miss_class', tasks: [{ taxonomy: 'review_quality' }] },
            ]}
            failureStageDistribution={{
              stages: { lint: 9, test: 7, build: 5, review: 4, pr_creation: 3 },
              total_classified: 28,
              dominant_stage: 'lint',
            }}
            scopeConversionRates={{
              small: { created: 25, merged: 17, calibrated_in: 2, calibrated_out: 1 },
              medium: { created: 18, merged: 15, calibrated_in: 1, calibrated_out: 0 },
              large: { created: 8, merged: 1, calibrated_in: 0, calibrated_out: 2 },
            }}
            prevScopeConversionRates={{
              small: { created: 20, merged: 12 },
              medium: { created: 15, merged: 11 },
              large: { created: 6, merged: 2 },
            }}
            scopeMismatchSummary={{
              total_calibrated: 6,
              calibrated_success_rate: 0.67,
              by_original_scope: {
                small: { total: 2, completed: 1, failed: 1 },
                medium: { total: 2, completed: 2, failed: 0 },
                large: { total: 2, completed: 1, failed: 1 },
              },
              top_directions: [
                { direction_id: 'd-001', direction_text: 'refactor: extract long-running section builders from strategic handler module', calibrated_count: 3, total_tasks: 12, calibration_rate: 0.25 },
                { direction_id: 'd-002', direction_text: 'feat: drill-through from reviewer disagreements panel to task list', calibrated_count: 2, total_tasks: 8, calibration_rate: 0.25 },
              ],
            }}
            rejectionPatternSummary={{
              total_rejected: 5,
              top_notes: [
                { snippet: 'scope too broad — task touches files outside the declared cluster boundary', count: 3 },
                { snippet: 'missing test coverage for the new extraction component', count: 2 },
              ],
            }}
            trendHistory={[
              { failed_by_taxonomy: { scope_related: 3, review_quality: 2 } },
              { failed_by_taxonomy: { scope_related: 4, review_quality: 2 } },
              { failed_by_taxonomy: { scope_related: 5, review_quality: 3 } },
              { failed_by_taxonomy: { scope_related: 6, review_quality: 4 } },
            ]}
            activeTaxonomyFilter={null}
            activeStageFilter={null}
            onTaxonomyFilterChange={() => {}}
            onStageFilterChange={() => {}}
            onExpandFailures={() => {}}
          />,
        );
      });
    });
  });
});
