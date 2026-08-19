import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ProjectAlertDomainsSection } from './ProjectAlertDomainsSection';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('../api', () => ({
  fetchProjectAlertDomains: vi.fn(async () => ({
    project_id: 'proj-1',
    domains: [
      {
        domain: 'core',
        label: 'Core operator alerts',
        description: 'Always-on alerts for approvals, blocked work, failed runs, and local Strategic health.',
        enabled: true,
        configured: false,
        default_enabled: true,
        core: true,
      },
      {
        domain: 'runaway_cost_guard',
        label: 'Runaway cost guard',
        description: 'Usage spikes, disabled models, daily budget pressure, and provider account health checks.',
        enabled: true,
        configured: true,
        default_enabled: true,
        core: false,
      },
      {
        domain: 'self_hosted_runner_registration',
        label: 'Self-hosted runner registration',
        description: 'Connected device registration, runner heartbeat loss, stale capacity, and wake failures.',
        enabled: false,
        configured: true,
        default_enabled: false,
        core: false,
      },
    ],
  })),
  updateProjectAlertDomains: vi.fn(),
}));

describe('ProjectAlertDomainsSection viewport regression', () => {
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
    host?.remove();
    host = null;
  });

  async function mount() {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <div className="min-h-screen bg-workbench-shell p-2 text-neutral-100">
          <ProjectAlertDomainsSection projectId="proj-1" canSave />
        </div>,
      );
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
  }

  test('renders alert-domain controls without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, mount);
  });
});
