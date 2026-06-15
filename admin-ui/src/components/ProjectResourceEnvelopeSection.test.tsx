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
