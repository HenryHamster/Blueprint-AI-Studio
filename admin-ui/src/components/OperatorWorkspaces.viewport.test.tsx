import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import {
  ProjectWorkspace,
  RuntimeWorkspace,
  type RuntimeSection,
} from './OperatorWorkspaces';
import type { Project } from '../types/project';
import { expectMinTouchTargetAt, expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';

vi.mock('./ConsoleHome', () => ({
  ConsoleHome: () => <div className="min-h-[220px] rounded-md border border-neutral-800 p-3">health</div>,
}));

vi.mock('./WarmVmPanel', () => ({
  WarmVmPanel: () => <div className="min-h-[260px] text-sm text-neutral-300">VM Fleet</div>,
}));

vi.mock('./FleetRunnersPanel', () => ({
  FleetRunnersPanel: () => <div className="min-h-[260px] text-sm text-neutral-300">Runners</div>,
}));

vi.mock('./ComputeJobsPanel', () => ({
  ComputeJobsPanel: () => <div className="min-h-[320px] rounded-md border border-neutral-800 p-3">Compute jobs</div>,
  default: () => <div className="min-h-[320px] rounded-md border border-neutral-800 p-3">Compute jobs</div>,
}));

vi.mock('./DashboardHome', () => ({
  DashboardHome: () => <div className="min-h-[220px] rounded-md border border-neutral-800 p-3">analytics</div>,
}));

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

function runtimeProps(runtimeSection: RuntimeSection) {
  return {
    runtimeSection,
    onRuntimeSectionChange: vi.fn(),
    runtimeConfigSection: 'orchestrator' as const,
    onRuntimeConfigSectionChange: vi.fn(),
    selectedProjectId: 'proj-1',
    selectedProjectRepoLabel: 'org/repo',
    projects: [],
    tasks: [],
    scopedActiveCount: 1,
    scopedQueuedCount: 2,
    globalActiveCount: 3,
    globalQueuedCount: 4,
    userRole: 'admin' as const,
    apiHealthy: true,
    wsConnected: true,
    settingsRouterProps: {} as never,
    addToast: vi.fn(),
    onViewTask: vi.fn(),
    onViewTaskById: vi.fn(),
    onOpenFleet: vi.fn(),
    onCreateTask: vi.fn(),
    onLaunchCompute: vi.fn(),
    onDirectionClick: vi.fn(),
    onConfigureTargets: vi.fn(),
    onSpawnOrphanFix: vi.fn(),
    onPanelReviewPr: vi.fn(),
    onApproveExperiment: vi.fn(async () => {}),
    onDismissExperiment: vi.fn(async () => {}),
    onNavigateToKnowledge: vi.fn(),
    onRestartOrchestrator: vi.fn(async () => {}),
    versionLifecycle: null,
    versionLifecycleError: null,
    versionLifecycleLastSuccessAt: null,
    versionLifecycleRefreshing: false,
    refreshVersionLifecycle: vi.fn(async () => {}),
  };
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
    onOpenProjectMembers: vi.fn(),
    onViewFindings: vi.fn(),
    onViewKnowledge: vi.fn(),
    onNavigateToTask: vi.fn(),
  };
}

describe('RuntimeWorkspace viewport regression', () => {
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

  async function renderRuntime(section: RuntimeSection) {
    cleanupHost();
    host = document.createElement('div');
    host.style.height = '760px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<RuntimeWorkspace {...runtimeProps(section)} />);
    });
  }

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

  test('devices surface fits mobile width without nested-panel overflow', async () => {
    await expectNoOverflowAt(375, () => renderRuntime('devices'));
  });

  test('accounts surface fits mobile and desktop workspace viewports', async () => {
    await expectNoOverflowAt(375, () => renderRuntime('accounts'));
    await expectNoOverflowAt(1280, () => renderRuntime('accounts'), { height: 760 });
  });

  test('project manage surface stays reachable at mobile width', async () => {
    await expectNoOverflowAt(375, renderProjectManage);
    await expectNoTextTruncationAt(375, renderProjectManage, {
      height: 760,
      selector: '[aria-label="Project management sections"] *, [data-testid="settings-section"]',
    });
    await expectMinTouchTargetAt(375, renderProjectManage, { height: 760 });
  });

  test('settings surface satisfies required mobile viewport checks', async () => {
    await expectNoTextTruncationAt(375, () => renderRuntime('settings'), {
      height: 760,
      selector: '[aria-label="Runtime settings sections"] *, [data-testid="settings-section"]',
    });
    await expectMinTouchTargetAt(375, () => renderRuntime('settings'), { height: 760 });
  });

  test('settings surface keeps compact navigation on mobile', async () => {
    await expectNoOverflowAt(375, () => renderRuntime('settings'));
    expect(document.querySelector('[aria-label="Runtime settings sections"]')?.textContent).not.toContain('Global slots, retries');
  });

  test('compute surface does not duplicate the child panel header copy', async () => {
    await expectNoOverflowAt(1024, () => renderRuntime('compute'));
    expect(document.body.textContent).not.toContain('Active experiments, held approvals, and terminal run analysis.');
  });
});
