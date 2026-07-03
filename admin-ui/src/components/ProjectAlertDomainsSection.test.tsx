import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ProjectAlertDomainsSection } from './ProjectAlertDomainsSection';
import { fetchProjectAlertDomains, updateProjectAlertDomains } from '../api';

vi.mock('../api', () => ({
  fetchProjectAlertDomains: vi.fn(),
  updateProjectAlertDomains: vi.fn(),
}));

const domainResponse = {
  project_id: 'proj-1',
  domains: [
    {
      domain: 'core',
      label: 'Core',
      description: 'Always-on core operator alerts',
      enabled: true,
      configured: false,
      default_enabled: true,
      core: true,
    },
    {
      domain: 'warm_pool',
      label: 'Warm Pool',
      description: 'Warm pool capacity and health alerts',
      enabled: true,
      configured: true,
      default_enabled: false,
      core: false,
    },
  ],
};

describe('ProjectAlertDomainsSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchProjectAlertDomains).mockResolvedValue(domainResponse);
    vi.mocked(updateProjectAlertDomains).mockResolvedValue({
      ...domainResponse,
      domains: domainResponse.domains.map(domain => (
        domain.domain === 'warm_pool' ? { ...domain, enabled: false } : domain
      )),
    });
  });

  it('loads alert domains and disables core controls', async () => {
    render(<ProjectAlertDomainsSection projectId="proj-1" canSave />);

    expect(await screen.findByText('Warm Pool')).toBeInTheDocument();
    expect(screen.getByText('Core')).toBeInTheDocument();
    expect(screen.getByLabelText('Disable Core')).toBeDisabled();
  });

  it('patches a domain when an owner toggles it', async () => {
    const user = userEvent.setup();
    render(<ProjectAlertDomainsSection projectId="proj-1" canSave />);

    const toggle = await screen.findByLabelText('Disable Warm Pool');
    await user.click(toggle);

    await waitFor(() => {
      expect(updateProjectAlertDomains).toHaveBeenCalledWith('proj-1', {
        warm_pool: { enabled: false },
      });
    });
  });

  it('shows read-only text for non-owners', async () => {
    render(<ProjectAlertDomainsSection projectId="proj-1" canSave={false} />);

    expect(await screen.findByText('Warm Pool')).toBeInTheDocument();
    expect(screen.getByText(/Only project owners can change alert-domain settings/i)).toBeInTheDocument();
    expect(screen.getByLabelText('Disable Warm Pool')).toBeDisabled();
  });
});
