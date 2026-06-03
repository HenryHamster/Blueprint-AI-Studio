import { afterEach, describe, test, vi, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ProjectDashboard } from './ProjectDashboard';
import type { Project } from '../types/project';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';

// Spread the real module so statically-imported named exports
// (e.g. validateApprovalGatePath, pulled in by ProjectApprovalGateSection)
// still resolve under browser-mode ESM linking, then override only the
// network-touching functions. Returning a bare object literal omits those
// exports and makes the browser-mode (vite) suite fail to link the module.
vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api');
  return {
    ...actual,
    apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
    fetchApprovalGateConfig: vi.fn(async () => ({
      project_id: 'proj-1',
      approval_gate_config: { infra_files: [], critical_files: [] },
      is_default: true,
    })),
    updateApprovalGateConfig: vi.fn(async (projectId: string, config: { infra_files: string[]; critical_files: string[] }) => ({
      project_id: projectId,
      approval_gate_config: config,
      is_default: config.infra_files.length === 0 && config.critical_files.length === 0,
    })),
  };
});

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
    const parent = escapeHatch.parentElement;
    expect(parent).not.toBeNull();
    if (!parent) throw new Error('missing parent');
    const rect = parent.getBoundingClientRect();
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

  test('admin state: New button meets 44px touch target at 375px', async () => {
    localStorage.setItem('projectDashboardCollapsed', 'false');

    const projects = [
      makeProject({ id: 'p1', name: 'Platform', status: 'active' }),
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
            userRole="admin"
            onNewProject={() => {}}
          />,
        );
      });
      expect(host?.querySelector('button[title="Add new project"]')).not.toBeNull();
    }, { selector: 'button[title="Add new project"]' });

    localStorage.removeItem('projectDashboardCollapsed');
  });

  test('selected-project state: UserPlus button meets 44px touch target at 375px', async () => {
    localStorage.setItem('projectDashboardCollapsed', 'false');

    const projects = [
      makeProject({ id: 'p1', name: 'Platform', status: 'active', my_role: 'owner' }),
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
            selectedProjectId="p1"
            onSelectProject={() => {}}
            onToggleMembers={() => {}}
            onOpenSetupGuide={() => {}}
          />,
        );
      });
      expect(host?.querySelector('button[title="Project Members"]')).not.toBeNull();
      await act(async () => {
        await new Promise<void>(resolve => setTimeout(resolve, 0));
      });
      expect(host?.textContent).toContain('Approval-Gate Files');
    }, { selector: 'button[title="Project Members"]' });

    localStorage.removeItem('projectDashboardCollapsed');
  });

  test('setup-guide state: Settings button meets 44px touch target at 375px', async () => {
    localStorage.setItem('projectDashboardCollapsed', 'false');

    // A project created within the last hour with no completed tasks triggers the setup nudge
    const recentProject = makeProject({
      id: 'p1',
      name: 'New Project',
      created_at: new Date(Date.now() - 10 * 60 * 1000).toISOString(),
    });

    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProjectDashboard
            projects={[recentProject]}
            tasks={[]}
            selectedProjectId={null}
            onSelectProject={() => {}}
            onOpenSetupGuide={() => {}}
          />,
        );
      });
      expect(host?.querySelector('button[title="Complete project setup"]')).not.toBeNull();
    }, { selector: 'button[title="Complete project setup"]' });

    localStorage.removeItem('projectDashboardCollapsed');
  });

  test('owner delete confirmation fits and meets touch targets at 375px', async () => {
    localStorage.setItem('projectDashboardCollapsed', 'false');

    const projects = [
      makeProject({ id: 'p1', name: 'Platform', status: 'DEPLOYING', my_role: 'owner' }),
      makeProject({ id: 'p2', name: 'Mobile App', status: 'FAILED', my_role: 'owner', autonomous_enabled: false }),
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
            onProjectDeleted={() => {}}
          />,
        );
      });
      // Open the actions menu, then the delete confirmation, on the first card.
      const actionsBtn = host?.querySelector('button[title="Project actions"]') as HTMLButtonElement | null;
      expect(actionsBtn).not.toBeNull();
      await act(async () => { actionsBtn?.click(); });
      const deleteBtn = host?.querySelector('button[title="Delete project"]') as HTMLButtonElement | null;
      expect(deleteBtn).not.toBeNull();
      await act(async () => { deleteBtn?.click(); });
      expect(host?.querySelector('button[title="Confirm delete"]')).not.toBeNull();
    }, { selector: 'button[title="Confirm delete"], button[title="Cancel delete"]' });

    localStorage.removeItem('projectDashboardCollapsed');
  });

  test('owner pause action and Paused pill fit and meet touch targets at 375px', async () => {
    localStorage.setItem('projectDashboardCollapsed', 'false');

    const projects = [
      makeProject({ id: 'p1', name: 'Platform', status: 'active', my_role: 'owner', paused: true }),
      makeProject({ id: 'p2', name: 'Mobile App', status: 'active', my_role: 'owner', autonomous_enabled: false }),
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
            onProjectPaused={() => {}}
          />,
        );
      });
      // The paused pill renders on the paused card.
      expect(host?.textContent).toContain('Paused');
      // Open the actions menu on the first card to surface the Resume action.
      const actionsBtn = host?.querySelector('button[title="Project actions"]') as HTMLButtonElement | null;
      expect(actionsBtn).not.toBeNull();
      await act(async () => { actionsBtn?.click(); });
      expect(host?.querySelector('button[title^="Resume project"]')).not.toBeNull();
    }, { selector: 'button[title^="Resume project"], button[title^="Pause project"]' });

    localStorage.removeItem('projectDashboardCollapsed');
  });

  test('owner archive confirmation fits and meets touch targets at 375px', async () => {
    localStorage.setItem('projectDashboardCollapsed', 'false');

    const projects = [
      makeProject({ id: 'p1', name: 'Platform', status: 'DEPLOYING', my_role: 'owner' }),
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
            onProjectArchived={() => {}}
          />,
        );
      });
      const actionsBtn = host?.querySelector('button[title="Project actions"]') as HTMLButtonElement | null;
      expect(actionsBtn).not.toBeNull();
      await act(async () => { actionsBtn?.click(); });
      const archiveBtn = host?.querySelector('button[title^="Archive project"]') as HTMLButtonElement | null;
      expect(archiveBtn).not.toBeNull();
      await act(async () => { archiveBtn?.click(); });
      expect(host?.querySelector('button[title="Confirm archive"]')).not.toBeNull();
    }, { selector: 'button[title="Confirm archive"], button[title="Cancel archive"]' });

    localStorage.removeItem('projectDashboardCollapsed');
  });

  test('text labels do not truncate silently at 375px', async () => {
    localStorage.setItem('projectDashboardCollapsed', 'false');

    const projects = [
      makeProject({ id: 'p1', name: 'Platform', status: 'active' }),
      makeProject({ id: 'p2', name: 'Mobile App', status: 'paused', autonomous_enabled: false }),
      makeProject({ id: 'p3', name: 'Data Pipeline', status: 'active' }),
    ];

    await expectNoTextTruncationAt(375, async () => {
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

    localStorage.removeItem('projectDashboardCollapsed');
  });
});
