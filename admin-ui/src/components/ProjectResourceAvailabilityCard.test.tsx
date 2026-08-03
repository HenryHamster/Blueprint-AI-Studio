import { render, screen, within } from '@testing-library/react';
import { describe, expect, test } from 'vitest';
import { ProjectResourceAvailabilityCard } from './ProjectResourceAvailabilityCard';
import type { ProjectResourceSummary } from '../hooks/useProjectResourceSummary';

const DATA: ProjectResourceSummary = {
  project_id: 'project-a',
  policy: { execution_resources: 'project owner', attribution: 'launcher', managed_by: 'owner' },
  accounts: {
    total: 4,
    enabled: 3,
    enabled_below_cap: 2,
    at_cap: 1,
    project_active_tasks: 2,
    project_daily_task_count: 7,
    project_daily_task_cap: 30,
    project_max_concurrent_tasks: null,
  },
  runners: { total: 3, online: 2, offline: 1 },
};

describe('ProjectResourceAvailabilityCard', () => {
  test('renders only aggregate availability and the execution-attribution policy', () => {
    render(<ProjectResourceAvailabilityCard data={DATA} loading={false} error={null} />);
    const card = screen.getByTestId('project-resource-availability');

    expect(within(card).getByText('LLM')).toBeInTheDocument();
    expect(within(card).getAllByText('Configured')).toHaveLength(2);
    expect(within(card).getByText('Enabled')).toBeInTheDocument();
    expect(within(card).getByText('Below cap')).toBeInTheDocument();
    expect(within(card).queryByText('Available')).not.toBeInTheDocument();
    expect(within(card).getByText('At cap')).toBeInTheDocument();
    expect(within(card).getByText('2 active · 7 today')).toBeInTheDocument();
    expect(within(card).getByText('30 tasks/day')).toBeInTheDocument();
    expect(within(card).getByText('Unbounded')).toBeInTheDocument();
    expect(within(card).getByText('Online')).toBeInTheDocument();
    expect(within(card).getByText('Offline')).toBeInTheDocument();
    expect(within(card).getByText(/Cap headroom only; runtime readiness is checked at dispatch/)).toBeInTheDocument();
    expect(within(card).getByText(/Execution uses project resources; activity is attributed to the launcher; managed by the project owner/)).toBeInTheDocument();
    expect(within(card).queryByRole('button')).not.toBeInTheDocument();
  });

  test('shows bounded loading and generic error states without backend details', () => {
    const { rerender } = render(<ProjectResourceAvailabilityCard data={null} loading error={null} />);
    expect(screen.getByLabelText('Loading project resource availability')).toBeInTheDocument();

    rerender(<ProjectResourceAvailabilityCard data={null} loading={false} error="provider account secret-host failed" />);
    expect(screen.getByText('Project resource availability is unavailable.')).toBeInTheDocument();
    expect(screen.queryByText(/provider account secret-host/)).not.toBeInTheDocument();
  });

  test('distinguishes an unbounded enabled pool from no configured enabled accounts', () => {
    const { rerender } = render(
      <ProjectResourceAvailabilityCard data={DATA} loading={false} error={null} />,
    );
    expect(screen.getByText('Unbounded')).toBeInTheDocument();

    const noEnabledAccounts: ProjectResourceSummary = {
      ...DATA,
      accounts: {
        ...DATA.accounts,
        enabled: 0,
        enabled_below_cap: 0,
        at_cap: 0,
        project_daily_task_cap: null,
        project_max_concurrent_tasks: null,
      },
    };
    rerender(
      <ProjectResourceAvailabilityCard data={noEnabledAccounts} loading={false} error={null} />,
    );
    expect(screen.getAllByText('Not configured')).toHaveLength(2);
    expect(screen.queryByText('Unbounded')).not.toBeInTheDocument();
  });
});
