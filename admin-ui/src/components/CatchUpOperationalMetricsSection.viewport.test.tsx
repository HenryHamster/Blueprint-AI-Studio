import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { CatchUpOperationalMetricsSection } from './CatchUpOperationalMetricsSection';
import { expectNoOverflowAt } from '../test/viewport';

describe('CatchUpOperationalMetricsSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('renders dense operational-metrics strips without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <CatchUpOperationalMetricsSection
            directionProgress={[
              { direction_id: 'd-001', direction_text: 'refactor: extract section builders' },
            ]}
            reviewCapExhaustionSummary={{
              count: 4,
              avg_iterations: 5.3,
              total_cost_usd: 18.42,
              avg_cost_usd: 4.6,
              top_directions: [{ direction_id: 'd-001', count: 3 }],
            }}
            totalMergeRetriesSaved={7}
            mergeEarlyExitCount={5}
            retryTaskCount={6}
            retryOverheadCostUsd={22.18}
            avgRetriesBeforeFailure={2.4}
            retryWasteByDirection={[
              { direction_id: 'd-001', direction_text: 'refactor: extract section builders', retry_cost_usd: 14.2, retry_task_count: 4 },
            ]}
            retrySuccessCount={9}
            retryWastedRate={0.31}
            retryRecoveryCostUsd={8.75}
            resurrectionOutcomeSummary={{ total: 12, completed: 8, failed_again: 4, success_rate: 0.67 }}
            migrationHealth={{
              current_schema_revision: '9a0892a1b2c3',
              migration_tasks_in_window: 5,
              migration_tasks_succeeded: 4,
              migration_tasks_failed: 1,
              migration_errors_in_window: 2,
              auto_generated_migrations: 1,
              migration_slot_contention: {
                status: 'elevated',
                severity: 'critical',
                holder: {
                  task_id: 'holder-1',
                  title: 'Author migration',
                  started_at: '2026-06-06T10:00:00Z',
                  hold_seconds: 9000,
                },
                deferred_count: 3,
                longest_wait: {
                  task_id: 'waiter-1',
                  title: 'Queued migration waiter',
                  waiting_since: '2026-06-06T11:00:00Z',
                  wait_seconds: 5400,
                },
              },
            }}
            migrationSlotContention={{
              slot_held: true,
              holder_task_id: 't-9',
              holder_title: 'add index on task.project_id',
              holder_project_id: 'p-1',
              holder_held_seconds: 2 * 3600 + 12 * 60,
              waiting_count: 5,
              longest_wait_seconds: 51 * 60,
              threshold_exceeded: true,
            }}
            preventionEfficacy={{
              preventive_tasks_created: 10,
              preventive_tasks_merged: 7,
              preventive_tasks_failed: 3,
              preventive_merge_rate: 0.7,
              overall_merge_rate: 0.55,
              assessment: 'positive',
            }}
            releaseQualifierInfraMetrics={{
              qualifier_runs_total: 20,
              release_infra_failure_total: 3,
              audit_report_missing_count: 2,
              audit_report_parseable_but_unscoreable_count: 1,
              smoketest_report_missing_count: 1,
              release_infra_failure_streak_max: 2,
            }}
            retryPatternSummary={{
              total_multi_attempt_failures: 8,
              repeat_class_count: 5,
              top_patterns: [
                { sequence: 'timeout → timeout', count: 3 },
                { sequence: 'empty → empty', count: 2 },
              ],
            }}
            analysisOverflowSummary={{
              total_count: 4,
              by_scope: { small: 1, medium: 2, large: 1 },
              cost_usd: 6.3,
            }}
            provisionWasteCostUsd={11.9}
            provisioningOverheadPct={0.22}
            escalationOutcomeSummary={{
              total_attempted: 9,
              total_merged: 5,
              total_failed: 4,
              overall_success_rate: 0.56,
              by_path: {
                fresh_vm_retry: { attempted: 5, merged: 3, failed: 2, success_rate: 0.6 },
                model_upgrade: { attempted: 4, merged: 2, failed: 2, success_rate: 0.5 },
              },
            }}
            retrySuccessRateByClass={{
              agent_timeout_class: { merged: 6, failed: 2, success_rate: 0.75 },
              scope_empty_class: { merged: 3, failed: 3, success_rate: 0.5 },
            }}
          />,
        );
      });
    });
  });
});
