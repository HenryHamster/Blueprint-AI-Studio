import { afterEach, describe, test, vi, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ProjectDashboard } from './ProjectDashboard';
import type { Project } from '../types/project';
import { expectNoOverflowAt, expectMinTouchTargetAt } from '../test/viewport';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
}));

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    id: 'proj-1',
    name: 'Platform',
    status: 'active',
    github_owner: 'acme',
    github_repo: 'platform',
    autonomous_enabled: true,
    directions: [],
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

describe('ProjectDashboard — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('renders expanded project carousel without overflow at 375px', async () => {
    // Pre-expand the panel (collapsed by default)
    localStorage.setItem('projectDashboardCollapsed', 'false');

    const projects = [
      makeProject({ id: 'p1', name: 'Platform', status: 'active' }),
      makeProject({ id: 'p2', name: 'Mobile App', status: 'paused', autonomous_enabled: false }),
      makeProject({ id: 'p3', name: 'Data Pipeline', status: 'active' }),
    ];

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProjectDashboard
            projects={projects}
            tasks={[]}
            selectedProjectId={null}
            onSelectProject={() => {}}
          />,
        );
      });
    });

    // Find the actual escape-hatch element (carousel) and assert its
    // IN-bounds parent (the ProjectDashboard root) stays within 375px.
    // Asserting against host.parentElement (= document.body) is trivially
    // true because page.viewport(375) constrains body width.
    const escapeHatch = host?.querySelector('[data-viewport-allow-overflow]') as HTMLElement | null;
    expect(escapeHatch).not.toBeNull();
    const parent = escapeHatch!.parentElement;
    expect(parent).not.toBeNull();
    const rect = parent!.getBoundingClientRect();
    expect(rect.right).toBeLessThanOrEqual(388); // 375px + 12px host padding + 1px tolerance

    localStorage.removeItem('projectDashboardCollapsed');
  });

  test('interactive controls meet 44px touch target at 375px', async () => {
    localStorage.setItem('projectDashboardCollapsed', 'false');

    const projects = [
      makeProject({ id: 'p1', name: 'Platform', status: 'active' }),
      makeProject({ id: 'p2', name: 'Mobile App', status: 'paused', autonomous_enabled: false }),
    ];

    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProjectDashboard
            projects={projects}
            tasks={[]}
            selectedProjectId={null}
            onSelectProject={() => {}}
          />,
        );
      });
    }, { selector: 'button, a[href]' });

    localStorage.removeItem('projectDashboardCollapsed');
  });
});
