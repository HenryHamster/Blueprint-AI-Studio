import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ProjectWorkspace } from './OperatorWorkspaces';
import type { Project } from '../types/project';
import { expectMinTouchTargetAt, expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';

vi.mock('./SettingsSectionRouter', () => ({
  SettingsSectionRouter: ({ activeSection }: { activeSection: string }) => (
    <div className="min-h-[320px] rounded-md border border-neutral-800 p-3" data-testid="settings-section">
      {activeSection}
    </div>
  ),
}));

vi.mock('./DirectionsPanel', () => ({
  DirectionsPanel: ({ activeTab }: { activeTab: string }) => <div>{activeTab}</div>,
}));

function makeProject(): Project {
  return {
    id: 'proj-1',
    name: 'Blueprint Admin UI',
    status: 'active',
    github_owner: 'org',
    github_repo: 'repo',
    autonomous_enabled: true,
    created_at: '2026-06-30T10:00:00Z',
    my_role: 'owner',
    is_self_repo: false,
  } as Project;
}

function projectProps() {
  const project = makeProject();
  return {
    projectSection: 'manage' as const,
    onProjectSectionChange: vi.fn(),
    projectManageSection: 'overview' as const,
    onProjectManageSectionChange: vi.fn(),
    selectedProject: project,
    selectedProjectId: project.id,
    selectedProjectRepoLabel: `${project.github_owner}/${project.github_repo}`,
    projects: [project],
    settingsRouterProps: {} as never,
    addToast: vi.fn(),
    onOpenProjectManagement: vi.fn(),
    onViewFindings: vi.fn(),
    onViewKnowledge: vi.fn(),
    onNavigateToTask: vi.fn(),
  };
}

describe('ProjectWorkspace viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  function cleanupHost() {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
    }
    root = null;
    host?.remove();
    host = null;
  }

  afterEach(() => {
    cleanupHost();
  });

  async function renderProjectManage() {
    cleanupHost();
    host = document.createElement('div');
    host.style.height = '760px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<ProjectWorkspace {...projectProps()} />);
    });
  }

  test('project manage surface stays reachable at mobile width', async () => {
    await expectNoOverflowAt(375, renderProjectManage);
    await expectNoTextTruncationAt(375, renderProjectManage, {
      height: 760,
      selector: '[aria-label="Project management sections"] *, [data-testid="settings-section"]',
    });
    await expectMinTouchTargetAt(375, renderProjectManage, { height: 760 });
  });
});
