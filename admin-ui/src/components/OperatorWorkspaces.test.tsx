import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, test, vi } from 'vitest';
import {
  ProjectWorkspace,
  type ProjectManageSection,
  type ProjectSection,
  type RuntimeSection,
  type RuntimeSettingsSection,
} from './OperatorWorkspaces';
import { RuntimeWorkspace } from './RuntimeWorkspace';
import {
  PROJECT_POLICY_SECTIONS,
  RUNTIME_POLICY_SECTIONS,
  SETTINGS_SECTION_HOMES,
} from './OperatorWorkspaceSections';
import type { SectionId } from './settingsSections';
import type { Task } from './TaskCard';
import type { Project } from '../types/project';

const resourceSummaryHookMock = vi.hoisted(() => vi.fn(() => ({
  projectId: 'proj-1',
  loading: false,
  error: null,
  data: {
    project_id: 'proj-1',
    policy: { execution_resources: 'project owner', attribution: 'launcher', managed_by: 'owner' },
    accounts: { total: 3, enabled: 3, enabled_below_cap: 2, at_cap: 1, project_active_tasks: 1, project_daily_task_count: 4, project_daily_task_cap: 30, project_max_concurrent_tasks: 6 },
    runners: { total: 2, online: 1, offline: 1 },
  },
})));

vi.mock('../hooks/useProjectResourceSummary', () => ({
  PROJECT_RESOURCE_SUMMARY_POLL_MS: 30_000,
  useProjectResourceSummary: resourceSummaryHookMock,
}));

vi.mock('./DirectionsPanel', () => ({
  DirectionsPanel: ({ activeTab }: { activeTab: string }) => <div data-testid="directions-panel">{activeTab}</div>,
}));

vi.mock('./ConsoleHome', () => ({
  ConsoleHome: () => <div>runtime overview</div>,
}));

vi.mock('./WarmVmPanel', () => ({
  WarmVmPanel: () => <div>VM Fleet</div>,
}));

vi.mock('./FleetRunnersPanel', () => ({
  FleetRunnersPanel: ({ readOnly }: { readOnly?: boolean }) => <div data-testid="fleet-runners-panel" data-read-only={String(!!readOnly)}>Runners</div>,
}));

vi.mock('./ComputeJobsPanel', () => ({
  ComputeJobsPanel: ({ readOnly }: { readOnly?: boolean }) => <div data-testid="compute-jobs-panel" data-read-only={String(!!readOnly)}>Compute jobs</div>,
  CompactJobsTable: () => <div>Compact jobs table</div>,
  default: () => <div>Compute jobs</div>,
}));

vi.mock('./DashboardHome', () => ({
  DashboardHome: () => <div>analytics</div>,
}));

vi.mock('./VersionLifecyclePanel', () => ({
  VersionLifecyclePanel: ({
    data,
    readOnly,
    canManageProject,
    canManagePlatform,
  }: {
    data: { current_version?: { version_number: number } | null } | null;
    readOnly?: boolean;
    canManageProject?: boolean;
    canManagePlatform?: boolean;
  }) => (
    <div
      data-testid="version-lifecycle-panel"
      data-read-only={String(!!readOnly)}
      data-can-manage-project={String(!!canManageProject)}
      data-can-manage-platform={String(!!canManagePlatform)}
    >
      {data?.current_version ? `v${data.current_version.version_number}` : 'no version data'}
    </div>
  ),
}));

vi.mock('./CompletionDebtPanel', () => ({
  CompletionDebtPanel: () => <div>completion debt</div>,
}));

vi.mock('./RuntimeAccountsPanel', () => ({
  RuntimeAccountsPanel: () => <div data-testid="runtime-accounts-panel">account monitor</div>,
}));

vi.mock('./SettingsSectionRouter', () => ({
  SettingsSectionRouter: ({ activeSection }: { activeSection: string }) => (
    <div data-testid="settings-section">{activeSection}</div>
  ),
}));

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    id: 'proj-1',
    name: 'Project Alpha',
    status: 'active',
    github_owner: 'acme',
    github_repo: 'widgets',
    autonomous_enabled: true,
    created_at: '2026-06-30T10:00:00Z',
    my_role: 'owner',
    is_self_repo: false,
    ...overrides,
  } as Project;
}

function projectProps(overrides: {
  projectSection?: ProjectSection;
  projectManageSection?: ProjectManageSection;
  selectedProject?: Project | null;
  selectedProjectId?: string | null;
  onProjectSectionChange?: (section: ProjectSection) => void;
  onProjectManageSectionChange?: (section: ProjectManageSection) => void;
} = {}) {
  const selectedProject = overrides.selectedProject === undefined ? makeProject() : overrides.selectedProject;
  return {
    projectSection: overrides.projectSection ?? 'manage',
    onProjectSectionChange: overrides.onProjectSectionChange ?? vi.fn(),
    projectManageSection: overrides.projectManageSection ?? 'overview',
    onProjectManageSectionChange: overrides.onProjectManageSectionChange ?? vi.fn(),
    selectedProject,
    selectedProjectId: overrides.selectedProjectId === undefined ? selectedProject?.id ?? null : overrides.selectedProjectId,
    selectedProjectRepoLabel: selectedProject ? `${selectedProject.github_owner}/${selectedProject.github_repo}` : null,
    projects: selectedProject ? [selectedProject] : [],
    settingsRouterProps: {} as never,
    addToast: vi.fn(),
    onOpenProjectManagement: vi.fn(),
    onViewFindings: vi.fn(),
    onViewKnowledge: vi.fn(),
    onNavigateToTask: vi.fn(),
  };
}

function runtimeProps(overrides: {
  runtimeSection?: RuntimeSection;
  runtimeConfigSection?: RuntimeSettingsSection;
  userRole?: 'admin' | 'user' | null;
  selectedProjectId?: string | null;
  selectedProjectRepoLabel?: string | null;
  projectRole?: Project['my_role'];
  onRuntimeSectionChange?: (section: RuntimeSection) => void;
  onRuntimeConfigSectionChange?: (section: RuntimeSettingsSection) => void;
} = {}) {
  return {
    runtimeSection: overrides.runtimeSection ?? 'settings',
    onRuntimeSectionChange: overrides.onRuntimeSectionChange ?? vi.fn(),
    runtimeConfigSection: overrides.runtimeConfigSection ?? 'orchestrator',
    onRuntimeConfigSectionChange: overrides.onRuntimeConfigSectionChange ?? vi.fn(),
    selectedProjectId: overrides.selectedProjectId ?? 'proj-1',
    selectedProjectRepoLabel: overrides.selectedProjectRepoLabel ?? 'acme/widgets',
    projects: [makeProject({ my_role: overrides.projectRole ?? 'owner' })],
    tasks: [] as Task[],
    scopedActiveCount: 1,
    scopedQueuedCount: 2,
    globalActiveCount: 3,
    globalQueuedCount: 4,
    userRole: overrides.userRole ?? 'admin',
    apiHealthy: true,
    wsConnected: true,
    settingsRouterProps: {} as never,
    onViewTask: vi.fn(),
    onViewTaskById: vi.fn(),
    onOpenFleet: vi.fn(),
    onCreateTask: vi.fn(),
    onLaunchCompute: vi.fn(),
    onDirectionClick: vi.fn(),
    onConfigureTargets: vi.fn(),
    onSpawnOrphanFix: vi.fn(),
    onApproveExperiment: vi.fn(async () => {}),
    onDismissExperiment: vi.fn(async () => {}),
    onNavigateToKnowledge: vi.fn(),
    addToast: vi.fn(),
    onRestartOrchestrator: vi.fn(async () => {}),
    versionLifecycle: null,
    versionLifecycleError: null,
    versionLifecycleLastSuccessAt: null,
    versionLifecycleRefreshing: false,
    refreshVersionLifecycle: vi.fn(async () => {}),
  };
}

describe('Operator workspaces', () => {
  test('routes top-level Project and Runtime section selections through controlled callbacks', async () => {
    const user = userEvent.setup();
    const onProjectSectionChange = vi.fn();
    const onRuntimeSectionChange = vi.fn();

    render(<ProjectWorkspace {...projectProps({ onProjectSectionChange })} />);
    await user.click(within(screen.getByLabelText('Project sections')).getByRole('button', { name: 'Directions' }));
    expect(onProjectSectionChange).toHaveBeenCalledWith('directions');

    render(<RuntimeWorkspace {...runtimeProps({ runtimeSection: 'health', onRuntimeSectionChange })} />);
    await user.click(within(screen.getByLabelText('Runtime sections')).getByRole('button', { name: 'Compute' }));
    expect(onRuntimeSectionChange).toHaveBeenCalledWith('compute');
  });

  test('hides global runtime surfaces for ordinary users and falls back to project devices', async () => {
    const { rerender } = render(<RuntimeWorkspace {...runtimeProps({ userRole: 'user', runtimeConfigSection: 'deploy' })} />);

    const tabs = screen.getByLabelText('Runtime sections');
    expect(within(tabs).queryByRole('button', { name: 'Overview' })).not.toBeInTheDocument();
    expect(within(tabs).queryByRole('button', { name: 'Accounts' })).not.toBeInTheDocument();
    expect(within(tabs).queryByRole('button', { name: 'Analytics' })).not.toBeInTheDocument();
    expect(within(tabs).queryByRole('button', { name: 'Settings' })).not.toBeInTheDocument();
    expect(within(tabs).getByRole('button', { name: 'Devices' })).toBeInTheDocument();
    expect(await screen.findByTestId('fleet-runners-panel')).toBeInTheDocument();
    expect(screen.queryByText('VM Fleet')).not.toBeInTheDocument();

    rerender(<RuntimeWorkspace {...runtimeProps({ userRole: 'admin', runtimeConfigSection: 'deploy' })} />);
    expect(within(screen.getByLabelText('Runtime settings sections')).getByRole('button', { name: /Deploy/ })).toBeInTheDocument();
    expect(await screen.findByTestId('settings-section')).toHaveTextContent('deploy');
  });

  test('keeps project runtime panels visible but read-only for project viewers', async () => {
    const versionLifecycle = {
      current_version: { version_number: 7 },
      recent_versions: [],
      promotion_enabled: false,
      promotion_readiness: null,
    } as never;
    const { rerender } = render(
      <RuntimeWorkspace {...runtimeProps({ userRole: 'user', projectRole: 'viewer', runtimeSection: 'devices' })} />,
    );
    expect(await screen.findByTestId('fleet-runners-panel')).toHaveAttribute('data-read-only', 'true');

    rerender(<RuntimeWorkspace {...runtimeProps({ userRole: 'user', projectRole: 'viewer', runtimeSection: 'compute' })} />);
    expect(await screen.findByTestId('compute-jobs-panel')).toHaveAttribute('data-read-only', 'true');

    rerender(
      <RuntimeWorkspace
        {...runtimeProps({ userRole: 'user', projectRole: 'viewer', runtimeSection: 'version-lifecycle' })}
        versionLifecycle={versionLifecycle}
      />,
    );
    expect(await screen.findByTestId('version-lifecycle-panel')).toHaveAttribute('data-read-only', 'true');
  });

  test('maps owner, member, and viewer access to the backend operation floors', async () => {
    const versionLifecycle = {
      current_version: { version_number: 7 },
      recent_versions: [],
      promotion_enabled: false,
      promotion_readiness: null,
    } as never;
    const { rerender } = render(
      <RuntimeWorkspace {...runtimeProps({ userRole: 'user', projectRole: 'owner', runtimeSection: 'devices' })} />,
    );
    expect(await screen.findByTestId('fleet-runners-panel')).toHaveAttribute('data-read-only', 'false');

    rerender(<RuntimeWorkspace {...runtimeProps({ userRole: 'user', projectRole: 'member', runtimeSection: 'devices' })} />);
    expect(await screen.findByTestId('fleet-runners-panel')).toHaveAttribute('data-read-only', 'true');

    rerender(<RuntimeWorkspace {...runtimeProps({ userRole: 'user', projectRole: 'member', runtimeSection: 'compute' })} />);
    expect(await screen.findByTestId('compute-jobs-panel')).toHaveAttribute('data-read-only', 'false');

    rerender(
      <RuntimeWorkspace
        {...runtimeProps({ userRole: 'user', projectRole: 'member', runtimeSection: 'version-lifecycle' })}
        versionLifecycle={versionLifecycle}
      />,
    );
    const memberVersion = await screen.findByTestId('version-lifecycle-panel');
    expect(memberVersion).toHaveAttribute('data-read-only', 'false');
    expect(memberVersion).toHaveAttribute('data-can-manage-project', 'false');
    expect(memberVersion).toHaveAttribute('data-can-manage-platform', 'false');

    rerender(
      <RuntimeWorkspace
        {...runtimeProps({ userRole: 'user', projectRole: 'owner', runtimeSection: 'version-lifecycle' })}
        versionLifecycle={versionLifecycle}
      />,
    );
    const ownerVersion = await screen.findByTestId('version-lifecycle-panel');
    expect(ownerVersion).toHaveAttribute('data-can-manage-project', 'true');
    expect(ownerVersion).toHaveAttribute('data-can-manage-platform', 'false');

    rerender(
      <RuntimeWorkspace
        {...runtimeProps({ userRole: 'admin', projectRole: 'member', runtimeSection: 'version-lifecycle' })}
        versionLifecycle={versionLifecycle}
      />,
    );
    const adminMemberVersion = await screen.findByTestId('version-lifecycle-panel');
    expect(adminMemberVersion).toHaveAttribute('data-read-only', 'false');
    expect(adminMemberVersion).toHaveAttribute('data-can-manage-project', 'false');
    expect(adminMemberVersion).toHaveAttribute('data-can-manage-platform', 'true');

    rerender(<RuntimeWorkspace {...runtimeProps({ userRole: 'user', projectRole: 'viewer', runtimeSection: 'compute' })} />);
    expect(await screen.findByTestId('compute-jobs-panel')).toHaveAttribute('data-read-only', 'true');
  });

  test('shows the same read-only project resource summary to owner, member, and viewer personas', async () => {
    const { rerender } = render(
      <RuntimeWorkspace {...runtimeProps({ userRole: 'user', projectRole: 'owner', runtimeSection: 'devices' })} />,
    );
    expect(await screen.findByTestId('project-resource-availability')).toBeInTheDocument();
    expect(resourceSummaryHookMock).toHaveBeenLastCalledWith('proj-1', true);

    rerender(<RuntimeWorkspace {...runtimeProps({ userRole: 'user', projectRole: 'member', runtimeSection: 'compute' })} />);
    expect(await screen.findByTestId('project-resource-availability')).toBeInTheDocument();
    expect(resourceSummaryHookMock).toHaveBeenLastCalledWith('proj-1', true);

    rerender(<RuntimeWorkspace {...runtimeProps({ userRole: 'user', projectRole: 'viewer', runtimeSection: 'devices' })} />);
    const card = await screen.findByTestId('project-resource-availability');
    expect(card).toHaveTextContent('Execution uses project resources');
    expect(within(card).queryByRole('button')).not.toBeInTheDocument();
    expect(resourceSummaryHookMock).toHaveBeenLastCalledWith('proj-1', true);
  });

  test('withholds the resource summary when the selected project is not in the confirmed project list', () => {
    render(
      <RuntimeWorkspace
        {...runtimeProps({ userRole: 'user', projectRole: 'viewer', runtimeSection: 'devices', selectedProjectId: 'unrelated-project' })}
      />,
    );
    expect(screen.queryByTestId('project-resource-availability')).not.toBeInTheDocument();
    expect(resourceSummaryHookMock).toHaveBeenLastCalledWith(null, true);
  });

  test('enables resource polling only while Devices or Compute is the effective surface', () => {
    const { rerender } = render(
      <RuntimeWorkspace {...runtimeProps({ userRole: 'admin', projectRole: 'owner', runtimeSection: 'health' })} />,
    );
    expect(resourceSummaryHookMock).toHaveBeenLastCalledWith('proj-1', false);
    expect(screen.queryByTestId('project-resource-availability')).not.toBeInTheDocument();

    rerender(<RuntimeWorkspace {...runtimeProps({ userRole: 'admin', projectRole: 'owner', runtimeSection: 'compute' })} />);
    expect(resourceSummaryHookMock).toHaveBeenLastCalledWith('proj-1', true);
    expect(screen.getByTestId('project-resource-availability')).toBeInTheDocument();

    rerender(<RuntimeWorkspace {...runtimeProps({ userRole: 'admin', projectRole: 'owner', runtimeSection: 'analytics' })} />);
    expect(resourceSummaryHookMock).toHaveBeenLastCalledWith('proj-1', false);
    expect(screen.queryByTestId('project-resource-availability')).not.toBeInTheDocument();
  });

  test('keeps project settings unreachable when no project is selected', () => {
    render(
      <ProjectWorkspace
        {...projectProps({ selectedProject: null, selectedProjectId: null, projectManageSection: 'overview' })}
      />,
    );

    expect(screen.getByText('No project is selected for this workspace.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Members/ })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Project management sections')).not.toBeInTheDocument();
  });

  test('shows actionable project recovery when a project-scoped tab has no selected project', async () => {
    const user = userEvent.setup();
    const onOpenProjectManagement = vi.fn();
    const onProjectSectionChange = vi.fn();

    render(
      <ProjectWorkspace
        {...projectProps({
          selectedProject: null,
          selectedProjectId: null,
          projectSection: 'problem-model',
          onProjectSectionChange,
        })}
        onOpenProjectManagement={onOpenProjectManagement}
      />,
    );

    expect(screen.queryByTestId('directions-panel')).not.toBeInTheDocument();
    expect(screen.getByText('No project is selected for this workspace.')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /Manage projects/ }));
    expect(onOpenProjectManagement).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole('button', { name: /Project overview/ }));
    expect(onProjectSectionChange).toHaveBeenCalledWith('manage');
  });

  test('exposes access management from the project manage overview', async () => {
    const user = userEvent.setup();
    const onProjectManageSectionChange = vi.fn();

    render(<ProjectWorkspace {...projectProps({ onProjectManageSectionChange })} />);

    const accessButtons = screen.getAllByRole('button', { name: /Access/ });
    expect(accessButtons.length).toBeGreaterThanOrEqual(2);
    await user.click(accessButtons.at(-1)!);
    expect(onProjectManageSectionChange).toHaveBeenCalledWith('access');
  });

  test('rehosts project and runtime settings sections through SettingsSectionRouter', async () => {
    const { rerender } = render(
      <ProjectWorkspace {...projectProps({ projectSection: 'manage', projectManageSection: 'alert-domains' })} />,
    );
    expect(await screen.findByTestId('settings-section')).toHaveTextContent('alert-domains');

    rerender(<RuntimeWorkspace {...runtimeProps({ runtimeSection: 'accounts' })} />);
    expect(await screen.findByTestId('runtime-accounts-panel')).toHaveTextContent('account monitor');

    rerender(<RuntimeWorkspace {...runtimeProps({ runtimeSection: 'settings', runtimeConfigSection: 'provider-settings' })} />);
    expect(await screen.findByTestId('settings-section')).toHaveTextContent('provider-settings');
  });

  test('keeps every settings section assigned to one canonical workbench home', () => {
    const allSections: SectionId[] = [
      'general',
      'provider-routing',
      'provider-settings',
      'orchestrator',
      'deploy',
      'accounts',
      'notifications',
      'advanced',
      'build-config',
      'alert-domains',
      'auto-remediation',
      'issue-implementation',
      'auto-review',
      'resource-envelope',
      'branches',
      'access',
    ];
    const projectHomes = new Set<string>(PROJECT_POLICY_SECTIONS);
    const runtimeHomes = new Set<string>(RUNTIME_POLICY_SECTIONS);

    expect(Object.keys(SETTINGS_SECTION_HOMES).sort()).toEqual([...allSections].sort());
    for (const section of allSections) {
      const home = SETTINGS_SECTION_HOMES[section];
      expect(projectHomes.has(section)).toBe(home.surface === 'project');
      expect(runtimeHomes.has(section)).toBe(home.surface === 'runtime');
    }
    for (const section of PROJECT_POLICY_SECTIONS) {
      expect(SETTINGS_SECTION_HOMES[section]).toMatchObject({ surface: 'project', section });
    }
    for (const section of RUNTIME_POLICY_SECTIONS) {
      const home = SETTINGS_SECTION_HOMES[section];
      expect(home.surface).toBe('runtime');
      expect(home.section).toBe(section === 'accounts' ? 'accounts' : 'settings');
    }
  });

  test('routes each canonical project policy home through project manage', () => {
    const { rerender } = render(
      <ProjectWorkspace {...projectProps({ projectSection: 'manage', projectManageSection: 'overview' })} />,
    );

    for (const section of PROJECT_POLICY_SECTIONS) {
      rerender(<ProjectWorkspace {...projectProps({ projectSection: 'manage', projectManageSection: section })} />);
      expect(screen.getByTestId('settings-section')).toHaveTextContent(section);
    }
  });

  test('routes each canonical runtime policy home through accounts or runtime settings', () => {
    const { rerender } = render(<RuntimeWorkspace {...runtimeProps({ runtimeSection: 'accounts' })} />);
    expect(screen.getByTestId('runtime-accounts-panel')).toHaveTextContent('account monitor');

    for (const section of RUNTIME_POLICY_SECTIONS.filter((item): item is RuntimeSettingsSection => item !== 'accounts')) {
      rerender(<RuntimeWorkspace {...runtimeProps({ runtimeSection: 'settings', runtimeConfigSection: section })} />);
      expect(screen.getByTestId('settings-section')).toHaveTextContent(section);
    }
  });

  test('keeps runtime scope and health visible beside settings navigation', () => {
    render(
      <RuntimeWorkspace
        {...runtimeProps({
          runtimeSection: 'settings',
          runtimeConfigSection: 'notifications',
          userRole: 'admin',
        })}
        selectedProjectId={null}
        selectedProjectRepoLabel={null}
        apiHealthy={false}
        wsConnected={false}
      />,
    );

    const status = screen.getByTestId('runtime-settings-status');
    expect(status).toHaveTextContent('Runtime');
    expect(status).toHaveTextContent('check');
    expect(status).toHaveTextContent('Scope');
    expect(status).toHaveTextContent('Fleet-wide');
    expect(status).toHaveTextContent('API');
    expect(status).toHaveTextContent('degraded');
    expect(status).toHaveTextContent('Events');
    expect(status).toHaveTextContent('stale');
    expect(status).toHaveTextContent('Role');
    expect(status).toHaveTextContent('admin');
    expect(screen.getByTestId('settings-section')).toHaveTextContent('notifications');
  });

  test('swallows rejected onRestartOrchestrator without emitting unhandledrejection', async () => {
    const user = userEvent.setup();
    const unhandledRejections: PromiseRejectionEvent[] = [];
    const onUnhandledRejection = (e: PromiseRejectionEvent) => {
      e.preventDefault();
      unhandledRejections.push(e);
    };
    window.addEventListener('unhandledrejection', onUnhandledRejection);

    const error = new Error('restart failed');
    const onRestartOrchestrator = vi.fn().mockRejectedValue(error);

    render(
      <RuntimeWorkspace
        {...runtimeProps({ userRole: 'admin', runtimeSection: 'health' })}
        onRestartOrchestrator={onRestartOrchestrator}
      />,
    );

    await user.click(screen.getByRole('button', { name: 'Restart orchestrator' }));
    // Flush microtask queue so the rejected promise settles
    await new Promise(resolve => setTimeout(resolve, 0));

    expect(onRestartOrchestrator).toHaveBeenCalledTimes(1);
    expect(unhandledRejections).toHaveLength(0);

    window.removeEventListener('unhandledrejection', onUnhandledRejection);
  });

  test('surfaces API and WebSocket state inside the runtime overview workspace', () => {
    render(
      <RuntimeWorkspace
        {...runtimeProps({ runtimeSection: 'health' })}
        selectedProjectId={null}
        selectedProjectRepoLabel={null}
        apiHealthy={false}
        wsConnected={false}
      />,
    );

    expect(screen.getByText('Runtime overview')).toBeInTheDocument();
    expect(screen.getByText('API')).toBeInTheDocument();
    expect(screen.getByText('error')).toBeInTheDocument();
    expect(screen.getByText('WS')).toBeInTheDocument();
    expect(screen.getByText('stale')).toBeInTheDocument();
    expect(screen.getByText('fleet-wide')).toBeInTheDocument();
  });

  test('version-lifecycle section renders panel when data is present and hides tab when absent', async () => {
    const user = userEvent.setup();
    const onRuntimeSectionChange = vi.fn();

    // No version data: tab should not appear
    const { rerender } = render(
      <RuntimeWorkspace
        {...runtimeProps({ runtimeSection: 'health', onRuntimeSectionChange })}
        versionLifecycle={null}
      />,
    );
    const tabContainer = screen.getByLabelText('Runtime sections');
    expect(within(tabContainer).queryByRole('button', { name: 'Versions' })).not.toBeInTheDocument();

    // With version data: Versions tab appears
    rerender(
      <RuntimeWorkspace
        {...runtimeProps({ runtimeSection: 'health', onRuntimeSectionChange })}
        versionLifecycle={{ current_version: { version_number: 7, phase: 'stabilize', phase_changed_at: '2026-01-01T00:00:00Z', opened_at: '2026-01-01T00:00:00Z', qualification_attempts: 0, merged_task_count: 0, goal: null, goal_updated_at: null, goal_editor_info: null, phase_reasoning: null, qualification_state: null, last_qualification: null, active_candidate_sha: null, latest_audit: null, audit_evidence_state: 'none', stale_audit: null }, recent_versions: [], promotion_enabled: false, promotion_readiness: null }}
      />,
    );
    expect(within(screen.getByLabelText('Runtime sections')).getByRole('button', { name: 'Versions' })).toBeInTheDocument();

    // Navigate to version-lifecycle section
    await user.click(within(screen.getByLabelText('Runtime sections')).getByRole('button', { name: 'Versions' }));
    expect(onRuntimeSectionChange).toHaveBeenCalledWith('version-lifecycle');

    rerender(
      <RuntimeWorkspace
        {...runtimeProps({ runtimeSection: 'version-lifecycle', onRuntimeSectionChange })}
        versionLifecycle={{ current_version: { version_number: 7, phase: 'stabilize', phase_changed_at: '2026-01-01T00:00:00Z', opened_at: '2026-01-01T00:00:00Z', qualification_attempts: 0, merged_task_count: 0, goal: null, goal_updated_at: null, goal_editor_info: null, phase_reasoning: null, qualification_state: null, last_qualification: null, active_candidate_sha: null, latest_audit: null, audit_evidence_state: 'none', stale_audit: null }, recent_versions: [], promotion_enabled: false, promotion_readiness: null }}
      />,
    );
    expect(await screen.findByTestId('version-lifecycle-panel')).toHaveTextContent('v7');
  });
});
