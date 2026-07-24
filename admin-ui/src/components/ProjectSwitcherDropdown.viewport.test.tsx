import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ProjectSwitcherDropdown } from './ProjectSwitcherDropdown';
import { expectNoOverflowAt, expectNoTextTruncationAt, expectMinTouchTargetAt } from '../test/viewport';
import type { Project } from '../types/project';

const PROJECTS: Project[] = [
  { id: 'proj-1', name: 'Blueprint AI Studio', status: 'active', github_owner: 'HenryHamster', github_repo: 'BlueprintAIStudio', autonomous_enabled: true, created_at: '2026-01-01T00:00:00Z' },
  { id: 'proj-2', name: 'Mobile Responsive Operator Dashboard Redesign', status: 'active', github_owner: 'HenryHamster', github_repo: 'dashboard-redesign', autonomous_enabled: false, created_at: '2026-01-01T00:00:00Z' },
  { id: 'proj-3', name: 'another-repo', status: 'active', github_owner: 'org', github_repo: 'another-repo', autonomous_enabled: false, created_at: '2026-01-01T00:00:00Z' },
];

describe('ProjectSwitcherDropdown — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) { host.remove(); host = null; }
  });

  function renderDropdown(r: Root) {
    r.render(
      <ProjectSwitcherDropdown
        projects={PROJECTS}
        selectedProjectId={null}
        onSelectProject={() => {}}
      />,
    );
  }

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '8px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { renderDropdown(root!); });
    });
  });

  test('project label text is not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '8px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { renderDropdown(root!); });
    });
  });

  test('trigger button meets 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '8px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { renderDropdown(root!); });
    });
  });
});
