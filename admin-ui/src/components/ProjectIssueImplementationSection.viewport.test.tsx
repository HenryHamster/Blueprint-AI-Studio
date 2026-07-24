import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import {
  expectNoOverflowAt,
  expectNoTextTruncationAt,
  expectMinTouchTargetAt,
} from '../test/viewport';
import { ProjectIssueImplementationSection } from './ProjectIssueImplementationSection';
import type { IssueImplementationConfig } from '../types/project';

const config: IssueImplementationConfig = {
  enabled: true,
  trigger_label: 'auto:implement',
  max_per_hour: 4,
  max_concurrent: 3,
  base_branch: 'main',
  default_effort: 'medium',
};

const renderSection = async (r: Root) => {
  await act(async () => {
    r.render(
      <ProjectIssueImplementationSection
        githubOwner="anthropic"
        githubRepo="blueprint-ai-studio"
        config={config}
        onSave={vi.fn(async () => {})}
      />,
    );
  });
};

describe('ProjectIssueImplementationSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) { const r = root; act(() => r.unmount()); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders without horizontal overflow at 375px with populated config', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderSection(r);
    });
  });

  test('repository / branch / label fields do not silently clip text at 375px', async () => {
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
