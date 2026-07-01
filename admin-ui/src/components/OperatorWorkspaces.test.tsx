import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, test, vi } from 'vitest';
import {
  ProjectWorkspace,
  RuntimeWorkspace,
  type ProjectManageSection,
  type ProjectSection,
  type RuntimeSection,
  type RuntimeSettingsSection,
} from './OperatorWorkspaces';
import type { Task } from './TaskCard';
import type { Project } from '../types/project';

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
  FleetRunnersPanel: () => <div>Runners</div>,
}));

vi.mock('./ComputeJobsPanel', () => ({
  ComputeJobsPanel: () => <div>Compute jobs</div>,
}));

vi.mock('./DashboardHome', () => ({
  DashboardHome: () => <div>analytics</div>,
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
    onOpenProjectMembers: vi.fn(),
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
    projects: [makeProject()],
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
    onPanelReviewPr: vi.fn(),
    onApproveExperiment: vi.fn(async () => {}),
    onDismissExperiment: vi.fn(async () => {}),
    onNavigateToKnowledge: vi.fn(),
    onRestartOrchestrator: vi.fn(async () => {}),
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

  test('shows admin-only runtime settings only for admins and falls back for non-admin selection', () => {
    const { rerender } = render(<RuntimeWorkspace {...runtimeProps({ userRole: 'user', runtimeConfigSection: 'deploy' })} />);

    const nav = screen.getByLabelText('Runtime settings sections');
    expect(within(nav).queryByRole('button', { name: /Deploy/ })).not.toBeInTheDocument();
    expect(within(nav).queryByRole('button', { name: /Provider routing/ })).not.toBeInTheDocument();
    expect(screen.getByTestId('settings-section')).toHaveTextContent('general');

    rerender(<RuntimeWorkspace {...runtimeProps({ userRole: 'admin', runtimeConfigSection: 'deploy' })} />);
    expect(within(screen.getByLabelText('Runtime settings sections')).getByRole('button', { name: /Deploy/ })).toBeInTheDocument();
    expect(screen.getByTestId('settings-section')).toHaveTextContent('deploy');
  });

  test('keeps project settings disabled when no project is selected', () => {
    render(
      <ProjectWorkspace
        {...projectProps({ selectedProject: null, selectedProjectId: null, projectManageSection: 'overview' })}
      />,
    );

    expect(screen.getAllByText('No project selected')).toHaveLength(2);
    expect(screen.queryByRole('button', { name: /Members/ })).not.toBeInTheDocument();
    expect(within(screen.getByLabelText('Project management sections')).getByRole('button', { name: /Build/ })).toBeDisabled();
    expect(within(screen.getByLabelText('Project management sections')).getByRole('button', { name: /Access/ })).toBeDisabled();
    expect(screen.getAllByRole('button', { name: /Build/ }).some(button => button.hasAttribute('disabled'))).toBe(true);
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

  test('rehosts project and runtime settings sections through SettingsSectionRouter', () => {
    const { rerender } = render(
      <ProjectWorkspace {...projectProps({ projectSection: 'manage', projectManageSection: 'alert-domains' })} />,
    );
    expect(screen.getByTestId('settings-section')).toHaveTextContent('alert-domains');

    rerender(<RuntimeWorkspace {...runtimeProps({ runtimeSection: 'accounts' })} />);
    expect(screen.getByTestId('runtime-accounts-panel')).toHaveTextContent('account monitor');

    rerender(<RuntimeWorkspace {...runtimeProps({ runtimeSection: 'settings', runtimeConfigSection: 'provider-settings' })} />);
    expect(screen.getByTestId('settings-section')).toHaveTextContent('provider-settings');
  });

  test('keeps runtime scope and health visible beside settings navigation', () => {
    render(
      <RuntimeWorkspace
        {...runtimeProps({
          runtimeSection: 'settings',
          runtimeConfigSection: 'notifications',
          userRole: 'user',
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
    expect(status).toHaveTextContent('user');
    expect(screen.getByTestId('settings-section')).toHaveTextContent('notifications');
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
});
