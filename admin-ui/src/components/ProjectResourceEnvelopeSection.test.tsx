import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ProjectResourceEnvelopeSection } from './ProjectResourceEnvelopeSection';

describe('ProjectResourceEnvelopeSection', () => {
  it('disables editing for non-owners', () => {
    render(
      <ProjectResourceEnvelopeSection
        githubOwner="myorg"
        githubRepo="myrepo"
        data={{
          project_id: 'project-1',
          max_concurrent_vms: 4,
          spend_ceiling_usd: 50,
          spend_window_days: 14,
          active_vm_count: 2,
          recent_spend_usd: 7.25,
          recent_spend_compute_usd: 4.0,
          recent_spend_agent_vm_usd: 2.25,
          recent_spend_idle_usd: 1.0,
          is_throttled: false,
        }}
        loading={false}
        error={null}
        canSave={false}
        onSave={vi.fn()}
      />,
    );

    expect(screen.getByText('Only project owners can edit resource envelopes.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save Resource Envelope' })).toBeDisabled();
  });

  it('shows Loading… in Configured Limits and Live Usage when data is null and loading', () => {
    render(
      <ProjectResourceEnvelopeSection
        githubOwner="myorg"
        githubRepo="myrepo"
        data={null}
        loading={true}
        error={null}
        canSave={false}
        onSave={vi.fn()}
      />,
    );

    expect(screen.getAllByText('Loading…').length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText('Unlimited')).not.toBeInTheDocument();
    expect(screen.queryByText(/\$0\.00/)).not.toBeInTheDocument();
  });

  it('shows Unavailable in Configured Limits and Live Usage when data is null and error is set', () => {
    render(
      <ProjectResourceEnvelopeSection
        githubOwner="myorg"
        githubRepo="myrepo"
        data={null}
        loading={false}
        error="Failed to load resource envelope"
        canSave={false}
        onSave={vi.fn()}
      />,
    );

    expect(screen.getAllByText('Unavailable').length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText('Unlimited')).not.toBeInTheDocument();
    expect(screen.queryByText(/\$0\.00/)).not.toBeInTheDocument();
  });

  it('shows stale data when data is present even if error is set', () => {
    render(
      <ProjectResourceEnvelopeSection
        githubOwner="myorg"
        githubRepo="myrepo"
        data={{
          project_id: 'project-1',
          max_concurrent_vms: 4,
          spend_ceiling_usd: 50,
          spend_window_days: 14,
          active_vm_count: 2,
          recent_spend_usd: 7.25,
          recent_spend_compute_usd: 4.0,
          recent_spend_agent_vm_usd: 2.25,
          recent_spend_idle_usd: 1.0,
          is_throttled: false,
        }}
        loading={false}
        error="Transient fetch error"
        canSave={false}
        onSave={vi.fn()}
      />,
    );

    expect(screen.queryByText('Unavailable')).not.toBeInTheDocument();
    expect(screen.getByText('4')).toBeInTheDocument();
    expect(screen.getByText('$50.00')).toBeInTheDocument();
  });

  it('renders the spend breakdown and a throttle banner when throttled', () => {
    render(
      <ProjectResourceEnvelopeSection
        githubOwner="myorg"
        githubRepo="myrepo"
        data={{
          project_id: 'project-1',
          max_concurrent_vms: 4,
          spend_ceiling_usd: 6,
          spend_window_days: 14,
          active_vm_count: 2,
          recent_spend_usd: 7.25,
          recent_spend_compute_usd: 4,
          recent_spend_agent_vm_usd: 2.25,
          recent_spend_idle_usd: 1,
          is_throttled: true,
        }}
        loading={false}
        error={null}
        canSave={false}
        onSave={vi.fn()}
      />,
    );

    expect(screen.getByText('Compute jobs')).toBeInTheDocument();
    expect(screen.getByText('Agent VMs')).toBeInTheDocument();
    expect(screen.getByText('Warm-pool idle')).toBeInTheDocument();
    expect(screen.getByText('$4.00')).toBeInTheDocument();
    expect(screen.getByText('$2.25')).toBeInTheDocument();
    expect(screen.getByText('$1.00')).toBeInTheDocument();
    expect(screen.getByText('$7.25 / $6.00')).toBeInTheDocument();
    expect(screen.getByText('Compute throttled — spend ceiling reached')).toBeInTheDocument();
  });

  it('does not render the throttle banner when not throttled', () => {
    render(
      <ProjectResourceEnvelopeSection
        githubOwner="myorg"
        githubRepo="myrepo"
        data={{
          project_id: 'project-1',
          max_concurrent_vms: 4,
          spend_ceiling_usd: 50,
          spend_window_days: 14,
          active_vm_count: 2,
          recent_spend_usd: 7.25,
          recent_spend_compute_usd: 4,
          recent_spend_agent_vm_usd: 2.25,
          recent_spend_idle_usd: 1,
          is_throttled: false,
        }}
        loading={false}
        error={null}
        canSave={false}
        onSave={vi.fn()}
      />,
    );

    expect(screen.queryByText('Compute throttled — spend ceiling reached')).not.toBeInTheDocument();
  });

  it('sends the edited resource-envelope fields on save', async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(
      <ProjectResourceEnvelopeSection
        githubOwner="myorg"
        githubRepo="myrepo"
        data={{
          project_id: 'project-1',
          max_concurrent_vms: 4,
          spend_ceiling_usd: 50,
          spend_window_days: 14,
          active_vm_count: 2,
          recent_spend_usd: 7.25,
          recent_spend_compute_usd: 4.0,
          recent_spend_agent_vm_usd: 2.25,
          recent_spend_idle_usd: 1.0,
          is_throttled: false,
        }}
        loading={false}
        error={null}
        canSave={true}
        onSave={onSave}
      />,
    );

    fireEvent.change(screen.getByLabelText('Max concurrent VMs'), { target: { value: '8' } });
    fireEvent.change(screen.getByLabelText('Spend ceiling (USD)'), { target: { value: '' } });
    fireEvent.change(screen.getByLabelText('Spend window (days)'), { target: { value: '21' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save Resource Envelope' }));

    expect(onSave).toHaveBeenCalledWith({
      max_concurrent_vms: 8,
      spend_ceiling_usd: null,
      spend_window_days: 21,
      max_concurrent_compute_jobs: null,
    });
  });
});
