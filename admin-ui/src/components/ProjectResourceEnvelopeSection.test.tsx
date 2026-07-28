import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
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
      spend_warning_fraction: null,
      allowed_machine_types: null,
      allowed_accelerator_types: null,
      pause_on_ceiling_exceeded: false,
    });
  });

  it('renders a checkbox per known machine type and accelerator type', () => {
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
        onSave={vi.fn()}
      />,
    );

    for (const type of ['e2-standard-2', 'e2-standard-4', 'e2-standard-8', 'e2-standard-16', 'e2-standard-32']) {
      expect(screen.getByRole('checkbox', { name: type })).toBeInTheDocument();
    }
    for (const type of ['T4', 'L4', 'V100']) {
      expect(screen.getByRole('checkbox', { name: type })).toBeInTheDocument();
    }
    expect(screen.getByRole('checkbox', { name: 'Block all GPU jobs' })).toBeInTheDocument();
  });

  it('pre-checks boxes matching the configured hardware policy', () => {
    render(
      <ProjectResourceEnvelopeSection
        githubOwner="myorg"
        githubRepo="myrepo"
        data={{
          project_id: 'project-1',
          max_concurrent_vms: 4,
          spend_ceiling_usd: 50,
          spend_window_days: 14,
          allowed_machine_types: ['e2-standard-2', 'e2-standard-8'],
          allowed_accelerator_types: ['T4'],
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
        onSave={vi.fn()}
      />,
    );

    expect(screen.getByRole('checkbox', { name: 'e2-standard-2' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'e2-standard-8' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'e2-standard-4' })).not.toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'T4' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'L4' })).not.toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'Block all GPU jobs' })).not.toBeChecked();
  });

  it('shows the configured hardware policy in the Configured Limits display', () => {
    render(
      <ProjectResourceEnvelopeSection
        githubOwner="myorg"
        githubRepo="myrepo"
        data={{
          project_id: 'project-1',
          max_concurrent_vms: 4,
          spend_ceiling_usd: 50,
          spend_window_days: 14,
          allowed_machine_types: ['e2-standard-2'],
          allowed_accelerator_types: [],
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

    // Machine types display shows the whitelist; accelerators show the all-blocked state.
    // (The same labels also appear in the editing hints, so assert presence, not uniqueness.)
    expect(screen.getAllByText('e2-standard-2').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('All GPU jobs blocked').length).toBeGreaterThanOrEqual(1);
    // An empty accelerator list pre-checks the "Block all GPU jobs" toggle.
    expect(screen.getByRole('checkbox', { name: 'Block all GPU jobs' })).toBeChecked();
  });

  it('sends the selected machine/accelerator whitelist on save', async () => {
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

    fireEvent.click(screen.getByRole('checkbox', { name: 'e2-standard-4' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'L4' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save Resource Envelope' }));

    expect(onSave).toHaveBeenCalledWith({
      max_concurrent_vms: 4,
      spend_ceiling_usd: 50,
      spend_window_days: 14,
      max_concurrent_compute_jobs: null,
      spend_warning_fraction: null,
      allowed_machine_types: ['e2-standard-4'],
      allowed_accelerator_types: ['L4'],
      pause_on_ceiling_exceeded: false,
    });
  });

  it('sends an empty accelerator list (block all GPUs), distinct from null', async () => {
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

    fireEvent.click(screen.getByRole('checkbox', { name: 'Block all GPU jobs' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save Resource Envelope' }));

    expect(onSave).toHaveBeenCalledWith({
      max_concurrent_vms: 4,
      spend_ceiling_usd: 50,
      spend_window_days: 14,
      max_concurrent_compute_jobs: null,
      spend_warning_fraction: null,
      allowed_machine_types: null,
      allowed_accelerator_types: [],
      pause_on_ceiling_exceeded: false,
    });
  });

  it('clears a prior policy to null when all boxes are unchecked', async () => {
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
          allowed_machine_types: ['e2-standard-2'],
          allowed_accelerator_types: ['T4'],
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

    fireEvent.click(screen.getByRole('checkbox', { name: 'e2-standard-2' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'T4' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save Resource Envelope' }));

    expect(onSave).toHaveBeenCalledWith({
      max_concurrent_vms: 4,
      spend_ceiling_usd: 50,
      spend_window_days: 14,
      max_concurrent_compute_jobs: null,
      spend_warning_fraction: null,
      allowed_machine_types: null,
      allowed_accelerator_types: null,
      pause_on_ceiling_exceeded: false,
    });
  });

  it('saves a selected broad execution placement policy', async () => {
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
          execution_placement_policy: 'managed_allowed',
          execution_placement_policy_is_explicit: true,
          active_vm_count: 0,
          recent_spend_usd: 0,
          recent_spend_compute_usd: 0,
          recent_spend_agent_vm_usd: 0,
          recent_spend_idle_usd: 0,
          is_throttled: false,
        }}
        loading={false}
        error={null}
        canSave
        onSave={onSave}
      />,
    );

    fireEvent.change(screen.getByLabelText('Execution placement policy'), {
      target: { value: 'local_only' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save Resource Envelope' }));

    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({
      execution_placement_policy: 'local_only',
    }));
    expect(onSave.mock.calls[0][0]).not.toHaveProperty('require_local_runner');
  });

  it('marks and explicitly migrates the legacy compute-experiment-only policy', async () => {
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
          require_local_runner: true,
          legacy_require_local_runner_active: true,
          execution_placement_policy: 'managed_allowed',
          execution_placement_policy_is_explicit: false,
          active_vm_count: 0,
          recent_spend_usd: 0,
          recent_spend_compute_usd: 0,
          recent_spend_agent_vm_usd: 0,
          recent_spend_idle_usd: 0,
          is_throttled: false,
        }}
        loading={false}
        error={null}
        canSave
        onSave={onSave}
      />,
    );

    expect(screen.getByText(/Legacy policy active/)).toBeInTheDocument();
    expect(screen.getByLabelText('Execution placement policy')).toHaveValue('legacy_compute_only');

    fireEvent.change(screen.getByLabelText('Execution placement policy'), {
      target: { value: 'local_preferred' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save Resource Envelope' }));

    expect(screen.getByText('Allow managed execution fallback?')).toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));

    await waitFor(() => expect(onSave).toHaveBeenCalledWith(expect.objectContaining({
      execution_placement_policy: 'local_preferred',
      require_local_runner: null,
    })));
  });

  it('requires confirmation before broadening an explicit local-only policy', async () => {
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
          execution_placement_policy: 'local_only',
          execution_placement_policy_is_explicit: true,
          active_vm_count: 0,
          recent_spend_usd: 0,
          recent_spend_compute_usd: 0,
          recent_spend_agent_vm_usd: 0,
          recent_spend_idle_usd: 0,
          is_throttled: false,
        }}
        loading={false}
        error={null}
        canSave
        onSave={onSave}
      />,
    );

    fireEvent.change(screen.getByLabelText('Execution placement policy'), {
      target: { value: 'managed_allowed' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save Resource Envelope' }));

    expect(screen.getByText('Allow managed execution fallback?')).toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('confirm-dialog-cancel'));
    expect(onSave).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Save Resource Envelope' }));
    fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith(expect.objectContaining({
      execution_placement_policy: 'managed_allowed',
    })));
  });
});
