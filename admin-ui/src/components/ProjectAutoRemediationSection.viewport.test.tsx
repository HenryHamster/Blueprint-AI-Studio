import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import {
  expectNoOverflowAt,
  expectNoTextTruncationAt,
  expectMinTouchTargetAt,
} from '../test/viewport';
import { ProjectAutoRemediationSection } from './ProjectAutoRemediationSection';
import type { AutoRemediationConfig, AutoRemediationStats } from '../types/project';

const config: AutoRemediationConfig = {
  enabled: true,
  branch_whitelist: ['main', 'master', 'release/2026.05', 'staging'],
  max_per_hour: 4,
  max_concurrent: 3,
  require_review: true,
};

const stats: AutoRemediationStats = {
  spawned_24h: 6,
  spawned_7d: 28,
  success_rate: 82,
  recent_tasks: [
    { id: 't-1', title: 'fix(ci): retry flaky agent test', status: 'COMPLETED', branch: 'remediate/agent-flake-1234', created_at: '2026-05-08T12:00:00Z' },
    { id: 't-2', title: 'fix(backend): import cycle in admin', status: 'IN_PROGRESS', branch: 'remediate/backend-import-5678', created_at: '2026-05-08T11:30:00Z' },
    { id: 't-3', title: 'fix(admin-ui): viewport overflow at 375px', status: 'EVALUATING', branch: 'remediate/viewport-9abc', created_at: '2026-05-08T10:45:00Z' },
    { id: 't-4', title: 'fix(infra): packer build flake', status: 'FAILED', branch: 'remediate/packer-def0', created_at: '2026-05-07T22:10:00Z' },
  ],
};

const renderSection = async (r: Root) => {
  await act(async () => {
    r.render(
      <ProjectAutoRemediationSection
        githubOwner="anthropic"
        githubRepo="blueprint-ai-studio"
        config={config}
        stats={stats}
        onSave={vi.fn(async () => {})}
      />,
    );
  });
};

describe('ProjectAutoRemediationSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) { const r = root; act(() => r.unmount()); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders without horizontal overflow at 375px with populated whitelist + stats', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderSection(r);
    });
  });

  test('long branch / task labels do not silently clip text at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderSection(r);
    });
  });

  test('interactive controls meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderSection(r);
    }, { selector: 'button, a[href]' });
  });
});
