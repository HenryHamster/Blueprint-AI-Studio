import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { CatchUpOperationalMetricsSection } from './CatchUpOperationalMetricsSection';

describe('CatchUpOperationalMetricsSection', () => {
  it('renders nothing when no metric data provided', () => {
    const { container } = render(
      <CatchUpOperationalMetricsSection directionProgress={[]} />
    );
    expect(container.textContent).toBe('');
  });

  it('renders the review cap exhaustion strip when data present', () => {
    render(
      <CatchUpOperationalMetricsSection
        directionProgress={[]}
        reviewCapExhaustionSummary={{
          count: 3,
          avg_iterations: 4.2,
          total_cost_usd: 12.5,
          avg_cost_usd: 4.17,
          top_directions: [{ direction_id: 'd-1', count: 2 }],
        }}
      />
    );
    expect(screen.getByText('Review cap exhaustion')).toBeInTheDocument();
    expect(screen.getByText(/3 tasks hit cap/)).toBeInTheDocument();
    expect(screen.getByText(/\$12\.50 wasted/)).toBeInTheDocument();
  });

  it('renders the early-exit strip with correct singular/plural', () => {
    render(
      <CatchUpOperationalMetricsSection
        directionProgress={[]}
        totalMergeRetriesSaved={1}
        mergeEarlyExitCount={1}
      />
    );
    expect(screen.getByText('Early exit')).toBeInTheDocument();
    expect(screen.getByText(/1 retry saved/)).toBeInTheDocument();
    expect(screen.getByText(/across 1 task/)).toBeInTheDocument();
  });

  it('renders the retry success by class strip with computed percentage', () => {
    render(
      <CatchUpOperationalMetricsSection
        directionProgress={[]}
        retrySuccessRateByClass={{
          lint_error: { merged: 3, failed: 1, success_rate: 0.75 },
        }}
      />
    );
    expect(screen.getByText('Retry success by class')).toBeInTheDocument();
    expect(screen.getByText(/\(75%\)/)).toBeInTheDocument();
  });

  it('renders the standalone provisioning overhead strip only when no provision waste', () => {
    const { rerender } = render(
      <CatchUpOperationalMetricsSection
        directionProgress={[]}
        provisioningOverheadPct={0.18}
      />
    );
    expect(screen.getByText('Provisioning overhead')).toBeInTheDocument();

    rerender(
      <CatchUpOperationalMetricsSection
        directionProgress={[]}
        provisionWasteCostUsd={5}
        provisioningOverheadPct={0.18}
      />
    );
    // Standalone strip suppressed; provision-waste strip shows the percentage inline instead
    expect(screen.getByText('Provision waste')).toBeInTheDocument();
    expect(screen.queryByText('Provisioning overhead')).not.toBeInTheDocument();
  });

  it('renders the migration slot strip with prolonged-hold highlight when threshold exceeded', () => {
    render(
      <CatchUpOperationalMetricsSection
        directionProgress={[]}
        migrationSlotContention={{
          slot_held: true,
          holder_task_id: 't-1',
          holder_title: 'add column',
          holder_project_id: 'p-1',
          holder_held_seconds: 2 * 3600 + 5 * 60,
          waiting_count: 5,
          longest_wait_seconds: 45 * 60,
          threshold_exceeded: true,
        }}
      />
    );
    expect(screen.getByText('Migration slot')).toBeInTheDocument();
    expect(screen.getByText(/held 2h 5m by add column/)).toBeInTheDocument();
    expect(screen.getByText(/5 waiting/)).toBeInTheDocument();
    expect(screen.getByText(/longest wait 45m/)).toBeInTheDocument();
    expect(screen.getByText(/prolonged hold/)).toBeInTheDocument();
  });

  it('hides the migration slot strip when the slot is free with no waiters', () => {
    render(
      <CatchUpOperationalMetricsSection
        directionProgress={[]}
        migrationSlotContention={{
          slot_held: false,
          holder_task_id: null,
          holder_title: null,
          holder_project_id: null,
          holder_held_seconds: null,
          waiting_count: 0,
          longest_wait_seconds: null,
          threshold_exceeded: false,
        }}
      />
    );
    expect(screen.queryByText('Migration slot')).not.toBeInTheDocument();
  });

  it('renders migration slot contention with an elevated highlight when waiters are stalled', () => {
    render(
      <CatchUpOperationalMetricsSection
        directionProgress={[]}
        migrationHealth={{
          current_schema_revision: '9a0892a1b2c3',
          migration_tasks_in_window: 2,
          migration_tasks_succeeded: 1,
          migration_tasks_failed: 0,
          migration_errors_in_window: 0,
          auto_generated_migrations: 0,
          migration_slot_contention: {
            status: 'elevated',
            severity: 'critical',
            holder: {
              task_id: 'holder-1',
              title: 'Author migration',
              started_at: '2026-06-06T10:00:00Z',
              hold_seconds: 9000,
            },
            deferred_count: 2,
            longest_wait: {
              task_id: 'waiter-1',
              title: 'Queued migration waiter',
              waiting_since: '2026-06-06T11:00:00Z',
              wait_seconds: 5400,
            },
          },
        }}
      />
    );

    expect(screen.getByText('Migration slot contention')).toBeInTheDocument();
    expect(screen.getByText(/Author migration held 2\.5h/)).toBeInTheDocument();
    expect(screen.getByText(/2 waiting/)).toBeInTheDocument();
    expect(screen.getByText(/longest wait 1\.5h/)).toBeInTheDocument();
  });
});
