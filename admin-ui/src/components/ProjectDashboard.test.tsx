import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ProjectDashboard } from './ProjectDashboard';
import type { Project } from '../types/project';
import type { Task } from './TaskCard';

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api');
  const approvalGateData = {
    project_id: 'proj-1',
    approval_gate_config: { infra_files: [], critical_files: [] },
    is_default: true,
  };
  return {
    ...actual,
    fetchApprovalGateConfig: vi.fn(async () => approvalGateData),
    updateApprovalGateConfig: vi.fn(async (projectId: string, config: { infra_files: string[]; critical_files: string[] }) => ({
      project_id: projectId,
      approval_gate_config: config,
      is_default: config.infra_files.length === 0 && config.critical_files.length === 0,
    })),
    deleteProject: vi.fn(async () => ({ message: 'deleted', active_tasks_cancelled: 0 })),
    resetProject: vi.fn(async () => ({ message: 'reset', active_tasks_cancelled: 0, tasks_deleted: 0 })),
    setProjectPaused: vi.fn(async (_projectId: string, paused: boolean) => ({ paused })),
    archiveProject: vi.fn(async () => ({ message: 'archived', archived: true })),
    unarchiveProject: vi.fn(async () => ({ message: 'unarchived', archived: false })),
    fetchArchivedProjects: vi.fn(async () => []),
  };
});

import { deleteProject, resetProject, setProjectPaused, archiveProject, unarchiveProject, fetchArchivedProjects } from '../api';

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    id: 'proj-1',
    name: 'Platform',
    status: 'active',
    github_owner: 'acme',
    github_repo: 'platform',
    autonomous_enabled: true,
    directions: [{ id: 'd-1', text: 'Ship the retry fix', priority: 'high', status: 'active', added_at: '2026-04-24T00:00:00Z' }],
    created_at: '2026-04-23T10:00:00Z',
    merge_verification_verdict: { state: 'ci_checks', test_commands: [] },
    ...overrides,
  };
}

function makeTask(project_id: string, status: Task['status']): Task {
  return {
    id: `${project_id}-${status}`,
    project_id,
    title: status,
    description: status,
    status,
    created_by: 'agent',
    requires_approval: false,
    created_at: '2026-04-24T11:00:00Z',
  };
}

describe('ProjectDashboard', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-04-24T12:00:00Z'));
    localStorage.clear();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it('returns null when there are no projects', () => {
    const { container } = render(
      <ProjectDashboard projects={[]} tasks={[]} selectedProjectId={null} onSelectProject={vi.fn()} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders rows, counts, and project actions when expanded', () => {
    localStorage.setItem('projectDashboardCollapsed', 'false');
    const onSelectProject = vi.fn();
    const onToggleMembers = vi.fn();
    const onNewProject = vi.fn();
    const onOpenSetupGuide = vi.fn();

    render(
      <ProjectDashboard
        projects={[makeProject(), makeProject({ id: 'proj-2', name: 'Bootstrap', github_repo: 'bootstrap', autonomous_enabled: false, created_at: '2026-04-24T11:30:00Z', directions: [] })]}
        tasks={[
          makeTask('proj-1', 'QUEUED'),
          makeTask('proj-1', 'IN_PROGRESS'),
          makeTask('proj-1', 'EVALUATING'),
          makeTask('proj-1', 'FAILED'),
          makeTask('proj-1', 'COMPLETED'),
        ]}
        selectedProjectId="proj-1"
        onSelectProject={onSelectProject}
        showMembers
        onToggleMembers={onToggleMembers}
        userRole="admin"
        onNewProject={onNewProject}
        onOpenSetupGuide={onOpenSetupGuide}
      />,
    );

    expect(screen.getByText('Projects')).toBeInTheDocument();
    expect(screen.getByText('(filtered)')).toBeInTheDocument();
    expect(screen.getByText('Platform')).toBeInTheDocument();
    expect(screen.getByText('acme/platform')).toBeInTheDocument();
    expect(screen.getByText('1 queued')).toBeInTheDocument();
    expect(screen.getByText('1 running')).toBeInTheDocument();
    expect(screen.getByText('1 evaluating')).toBeInTheDocument();
    expect(screen.getAllByText('1 failed')).toHaveLength(2);
    expect(screen.getByText('1 done')).toBeInTheDocument();
    expect(screen.getByText('Bootstrap')).toBeInTheDocument();
    expect(screen.getByTitle('Complete project setup')).toBeInTheDocument();
    expect(screen.getByTitle('Project Members')).toBeInTheDocument();
    expect(screen.getByTitle('Add new project')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Platform' }));
    expect(onSelectProject).toHaveBeenCalledWith(null);
    fireEvent.click(screen.getByTitle('Project Members'));
    expect(onToggleMembers).toHaveBeenCalled();
    fireEvent.click(screen.getByTitle('Complete project setup'));
    expect(onOpenSetupGuide).toHaveBeenCalledWith('proj-2');
    fireEvent.click(screen.getByTitle('Add new project'));
    expect(onNewProject).toHaveBeenCalled();
  });

  it('renders a blocked task count distinct from queued', () => {
    localStorage.setItem('projectDashboardCollapsed', 'false');
    render(
      <ProjectDashboard
        projects={[makeProject()]}
        tasks={[
          makeTask('proj-1', 'QUEUED'),
          makeTask('proj-1', 'BLOCKED'),
          makeTask('proj-1', 'BLOCKED'),
        ]}
        selectedProjectId="proj-1"
        onSelectProject={vi.fn()}
      />,
    );
    expect(screen.getByText('1 queued')).toBeInTheDocument();
    expect(screen.getByText('2 blocked')).toBeInTheDocument();
  });

  it('renders a per-project status badge', () => {
    localStorage.setItem('projectDashboardCollapsed', 'false');
    render(
      <ProjectDashboard
        projects={[makeProject({ status: 'DEPLOYING' }), makeProject({ id: 'proj-2', name: 'Bootstrap', status: 'FAILED' })]}
        tasks={[]}
        selectedProjectId={null}
        onSelectProject={vi.fn()}
      />,
    );
    expect(screen.getByText('Deploying')).toBeInTheDocument();
    expect(screen.getByText('Failed')).toBeInTheDocument();
  });

  it('renders the merge-gate posture badge for each verdict state', () => {
    localStorage.setItem('projectDashboardCollapsed', 'false');
    render(
      <ProjectDashboard
        projects={[
          makeProject({
            id: 'proj-ci',
            name: 'CI Project',
            merge_verification_verdict: { state: 'ci_checks', test_commands: [] },
          }),
          makeProject({
            id: 'proj-tests',
            name: 'Tests Project',
            merge_verification_verdict: {
              state: 'tests_gated',
              test_commands: [{ path: 'backend/', test_cmd: 'pytest' }],
            },
          }),
          makeProject({
            id: 'proj-unverified',
            name: 'Unverified Project',
            merge_verification_verdict: { state: 'unverified', test_commands: [] },
          }),
          makeProject({
            id: 'proj-stall',
            name: 'Stall Risk Project',
            build_config: { has_ci: false, requires_ci_checks: true },
            merge_verification_verdict: { state: 'ci_checks', test_commands: [] },
          }),
        ]}
        tasks={[]}
        selectedProjectId={null}
        onSelectProject={vi.fn()}
      />,
    );

    expect(screen.getAllByText('CI gated')).toHaveLength(1);
    expect(screen.getByText('Tests gated')).toBeInTheDocument();
    expect(screen.getByText('UNVERIFIED')).toBeInTheDocument();
    expect(screen.getByText('Stall risk')).toBeInTheDocument();
  });

  it('keeps the tests-gated badge green when no runtime block is present', () => {
    localStorage.setItem('projectDashboardCollapsed', 'false');
    render(
      <ProjectDashboard
        projects={[makeProject({
          id: 'proj-tests',
          name: 'Tests Project',
          merge_verification_verdict: {
            state: 'tests_gated',
            test_commands: [{ path: 'backend/', test_cmd: 'pytest' }],
          },
        })]}
        tasks={[]}
        selectedProjectId={null}
        onSelectProject={vi.fn()}
      />,
    );

    const badge = screen.getByText('Tests gated');
    expect(badge).toHaveAttribute('data-runtime-state', 'absent');
    expect(badge).toHaveAttribute('title', expect.stringContaining("Blueprint will run the project's detected test command(s) before merging PRs."));
  });

  it('shows a warning badge when the live gate last fell back to UNVERIFIED', () => {
    localStorage.setItem('projectDashboardCollapsed', 'false');
    render(
      <ProjectDashboard
        projects={[makeProject({
          id: 'proj-tests',
          name: 'Tests Project',
          merge_verification_verdict: {
            state: 'tests_gated',
            test_commands: [{ path: 'backend/', test_cmd: 'pytest' }],
          },
          merge_gate_runtime: {
            last_run_at: '2026-04-24T11:00:00Z',
            executed_real_tests: true,
            last_unverified_at: '2026-04-24T11:30:00Z',
            last_unverified_reason: 'no_test_command_detected',
          },
        })]}
        tasks={[]}
        selectedProjectId={null}
        onSelectProject={vi.fn()}
      />,
    );

    const badge = screen.getByText('Tests gated?');
    expect(badge).toHaveAttribute('data-runtime-state', 'unverified-fallback');
    expect(badge).toHaveAttribute('title', expect.stringContaining('UNVERIFIED'));
    expect(badge).toHaveAttribute('title', expect.stringContaining('no test command detected'));
  });

  it('counts paused projects from the paused flag in the summary strip', () => {
    localStorage.setItem('projectDashboardCollapsed', 'false');
    render(
      <ProjectDashboard
        projects={[
          makeProject(),
          makeProject({ id: 'proj-2', name: 'Paused Platform', status: 'active', paused: true }),
        ]}
        tasks={[]}
        selectedProjectId={null}
        onSelectProject={vi.fn()}
      />,
    );

    expect(screen.getByText('1 paused')).toBeInTheDocument();
  });

  it('shows the actions menu (with Delete) only for project owners', () => {
    localStorage.setItem('projectDashboardCollapsed', 'false');
    const { rerender } = render(
      <ProjectDashboard
        projects={[makeProject({ my_role: 'member' })]}
        tasks={[]}
        selectedProjectId={null}
        onSelectProject={vi.fn()}
      />,
    );
    expect(screen.queryByTitle('Project actions')).not.toBeInTheDocument();

    rerender(
      <ProjectDashboard
        projects={[makeProject({ my_role: 'owner' })]}
        tasks={[]}
        selectedProjectId={null}
        onSelectProject={vi.fn()}
      />,
    );
    expect(screen.getByTitle('Project actions')).toBeInTheDocument();
    fireEvent.click(screen.getByTitle('Project actions'));
    expect(screen.getByTitle('Delete project')).toBeInTheDocument();
  });

  it('confirmation step names the project and warns active work is cancelled', () => {
    localStorage.setItem('projectDashboardCollapsed', 'false');
    render(
      <ProjectDashboard
        projects={[makeProject({ my_role: 'owner', name: 'Platform' })]}
        tasks={[]}
        selectedProjectId={null}
        onSelectProject={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTitle('Project actions'));
    fireEvent.click(screen.getByTitle('Delete project'));
    expect(screen.getByTitle('Confirm delete')).toBeInTheDocument();
    expect(screen.getByText(/force-cancelled/)).toBeInTheDocument();
    // Name appears in the card header AND in the confirmation warning.
    expect(screen.getAllByText('Platform').length).toBeGreaterThanOrEqual(2);
  });

  it('calls deleteProject(force) and onProjectDeleted on success', async () => {
    vi.useRealTimers();
    localStorage.setItem('projectDashboardCollapsed', 'false');
    const onProjectDeleted = vi.fn();
    render(
      <ProjectDashboard
        projects={[makeProject({ my_role: 'owner', created_at: '2020-01-01T00:00:00Z' })]}
        tasks={[]}
        selectedProjectId={null}
        onSelectProject={vi.fn()}
        onProjectDeleted={onProjectDeleted}
      />,
    );
    fireEvent.click(screen.getByTitle('Project actions'));
    fireEvent.click(screen.getByTitle('Delete project'));
    fireEvent.click(screen.getByTitle('Confirm delete'));

    await vi.waitFor(() => expect(onProjectDeleted).toHaveBeenCalledWith('proj-1'));
    expect(deleteProject).toHaveBeenCalledWith('proj-1', true);
  });

  it('surfaces a delete error inline and does not call onProjectDeleted', async () => {
    vi.useRealTimers();
    localStorage.setItem('projectDashboardCollapsed', 'false');
    vi.mocked(deleteProject).mockRejectedValueOnce(new Error('Permission denied'));
    const onProjectDeleted = vi.fn();
    render(
      <ProjectDashboard
        projects={[makeProject({ my_role: 'owner', created_at: '2020-01-01T00:00:00Z' })]}
        tasks={[]}
        selectedProjectId={null}
        onSelectProject={vi.fn()}
        onProjectDeleted={onProjectDeleted}
      />,
    );
    fireEvent.click(screen.getByTitle('Project actions'));
    fireEvent.click(screen.getByTitle('Delete project'));
    fireEvent.click(screen.getByTitle('Confirm delete'));

    expect(await screen.findByText('Permission denied')).toBeInTheDocument();
    expect(onProjectDeleted).not.toHaveBeenCalled();
  });

  it('reset confirmation states what is wiped vs kept, and confirming calls resetProject', async () => {
    vi.useRealTimers();
    localStorage.setItem('projectDashboardCollapsed', 'false');
    const onProjectReset = vi.fn();
    render(
      <ProjectDashboard
        projects={[makeProject({ my_role: 'owner', name: 'Platform', created_at: '2020-01-01T00:00:00Z' })]}
        tasks={[]}
        selectedProjectId={null}
        onSelectProject={vi.fn()}
        onProjectReset={onProjectReset}
      />,
    );
    fireEvent.click(screen.getByTitle('Project actions'));
    fireEvent.click(screen.getByTitle(/Reset project to a clean/));
    // Confirmation spells out wipe vs keep.
    expect(screen.getByText(/all task history and runtime state/)).toBeInTheDocument();
    expect(screen.getByText(/the project and all its configuration/)).toBeInTheDocument();

    fireEvent.click(screen.getByTitle('Confirm reset'));
    await vi.waitFor(() => expect(onProjectReset).toHaveBeenCalledWith('proj-1'));
    expect(resetProject).toHaveBeenCalledWith('proj-1');
  });

  it('owner can pause a project: action calls setProjectPaused(true) and onProjectPaused', async () => {
    vi.useRealTimers();
    localStorage.setItem('projectDashboardCollapsed', 'false');
    const onProjectPaused = vi.fn();
    render(
      <ProjectDashboard
        projects={[makeProject({ my_role: 'owner', created_at: '2020-01-01T00:00:00Z' })]}
        tasks={[]}
        selectedProjectId={null}
        onSelectProject={vi.fn()}
        onProjectPaused={onProjectPaused}
      />,
    );
    fireEvent.click(screen.getByTitle('Project actions'));
    fireEvent.click(screen.getByTitle(/Pause project/));

    await vi.waitFor(() => expect(onProjectPaused).toHaveBeenCalledWith('proj-1'));
    expect(setProjectPaused).toHaveBeenCalledWith('proj-1', true);
  });

  it('renders a Paused pill and a Resume action that calls setProjectPaused(false)', async () => {
    vi.useRealTimers();
    localStorage.setItem('projectDashboardCollapsed', 'false');
    const onProjectPaused = vi.fn();
    render(
      <ProjectDashboard
        projects={[makeProject({ my_role: 'owner', paused: true, created_at: '2020-01-01T00:00:00Z' })]}
        tasks={[]}
        selectedProjectId={null}
        onSelectProject={vi.fn()}
        onProjectPaused={onProjectPaused}
      />,
    );
    // The distinct paused pill is visible on the card.
    expect(screen.getByText('Paused')).toBeInTheDocument();

    fireEvent.click(screen.getByTitle('Project actions'));
    fireEvent.click(screen.getByTitle(/Resume project/));

    await vi.waitFor(() => expect(onProjectPaused).toHaveBeenCalledWith('proj-1'));
    expect(setProjectPaused).toHaveBeenCalledWith('proj-1', false);
  });

  it('surfaces a pause error inline and does not call onProjectPaused', async () => {
    vi.useRealTimers();
    localStorage.setItem('projectDashboardCollapsed', 'false');
    vi.mocked(setProjectPaused).mockRejectedValueOnce(new Error('Permission denied'));
    const onProjectPaused = vi.fn();
    render(
      <ProjectDashboard
        projects={[makeProject({ my_role: 'owner', created_at: '2020-01-01T00:00:00Z' })]}
        tasks={[]}
        selectedProjectId={null}
        onSelectProject={vi.fn()}
        onProjectPaused={onProjectPaused}
      />,
    );
    fireEvent.click(screen.getByTitle('Project actions'));
    fireEvent.click(screen.getByTitle(/Pause project/));

    expect(await screen.findByText('Permission denied')).toBeInTheDocument();
    expect(onProjectPaused).not.toHaveBeenCalled();
  });

  it('non-owners see no pause control', () => {
    localStorage.setItem('projectDashboardCollapsed', 'false');
    render(
      <ProjectDashboard
        projects={[makeProject({ my_role: 'member' })]}
        tasks={[]}
        selectedProjectId={null}
        onSelectProject={vi.fn()}
      />,
    );
    expect(screen.queryByTitle('Project actions')).not.toBeInTheDocument();
    expect(screen.queryByTitle(/Pause project/)).not.toBeInTheDocument();
  });

  it('owner menu shows Archive; confirming calls archiveProject and onProjectArchived', async () => {
    vi.useRealTimers();
    localStorage.setItem('projectDashboardCollapsed', 'false');
    const onProjectArchived = vi.fn();
    render(
      <ProjectDashboard
        projects={[makeProject({ my_role: 'owner', name: 'Platform', created_at: '2020-01-01T00:00:00Z' })]}
        tasks={[]}
        selectedProjectId={null}
        onSelectProject={vi.fn()}
        onProjectArchived={onProjectArchived}
      />,
    );
    fireEvent.click(screen.getByTitle('Project actions'));
    fireEvent.click(screen.getByTitle(/Archive project/));
    // Confirmation explains it's reversible and hides the project.
    expect(screen.getByText(/hidden from the active dashboard/)).toBeInTheDocument();
    expect(screen.getByText(/Reversible/)).toBeInTheDocument();

    fireEvent.click(screen.getByTitle('Confirm archive'));
    await vi.waitFor(() => expect(onProjectArchived).toHaveBeenCalledWith('proj-1'));
    expect(archiveProject).toHaveBeenCalledWith('proj-1');
  });

  it('surfaces an archive error inline and does not call onProjectArchived', async () => {
    vi.useRealTimers();
    localStorage.setItem('projectDashboardCollapsed', 'false');
    vi.mocked(archiveProject).mockRejectedValueOnce(new Error('Permission denied'));
    const onProjectArchived = vi.fn();
    render(
      <ProjectDashboard
        projects={[makeProject({ my_role: 'owner', created_at: '2020-01-01T00:00:00Z' })]}
        tasks={[]}
        selectedProjectId={null}
        onSelectProject={vi.fn()}
        onProjectArchived={onProjectArchived}
      />,
    );
    fireEvent.click(screen.getByTitle('Project actions'));
    fireEvent.click(screen.getByTitle(/Archive project/));
    fireEvent.click(screen.getByTitle('Confirm archive'));

    expect(await screen.findByText('Permission denied')).toBeInTheDocument();
    expect(onProjectArchived).not.toHaveBeenCalled();
  });

  it('Show archived toggle fetches archived projects and renders them', async () => {
    vi.useRealTimers();
    localStorage.setItem('projectDashboardCollapsed', 'false');
    vi.mocked(fetchArchivedProjects).mockResolvedValueOnce([
      makeProject({ id: 'arch-1', name: 'Old Project', github_repo: 'old', archived: true, my_role: 'owner' }),
    ]);
    render(
      <ProjectDashboard
        projects={[makeProject({ my_role: 'owner' })]}
        tasks={[]}
        selectedProjectId={null}
        onSelectProject={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTitle('Show archived projects'));
    expect(fetchArchivedProjects).toHaveBeenCalled();
    expect(await screen.findByText('Old Project')).toBeInTheDocument();
    expect(screen.getByText('Archived projects')).toBeInTheDocument();
  });

  it('Restore calls unarchiveProject and onProjectUnarchived', async () => {
    vi.useRealTimers();
    localStorage.setItem('projectDashboardCollapsed', 'false');
    vi.mocked(fetchArchivedProjects).mockResolvedValue([
      makeProject({ id: 'arch-1', name: 'Old Project', github_repo: 'old', archived: true, my_role: 'owner' }),
    ]);
    const onProjectUnarchived = vi.fn();
    render(
      <ProjectDashboard
        projects={[makeProject({ my_role: 'owner' })]}
        tasks={[]}
        selectedProjectId={null}
        onSelectProject={vi.fn()}
        onProjectUnarchived={onProjectUnarchived}
      />,
    );
    fireEvent.click(screen.getByTitle('Show archived projects'));
    const restoreBtn = await screen.findByTitle('Restore project to the active list');
    fireEvent.click(restoreBtn);
    await vi.waitFor(() => expect(onProjectUnarchived).toHaveBeenCalledWith('arch-1'));
    expect(unarchiveProject).toHaveBeenCalledWith('arch-1');
  });

  it('hides the admin new-project control for non-admin users', () => {
    localStorage.setItem('projectDashboardCollapsed', 'false');
    render(
      <ProjectDashboard
        projects={[makeProject()]}
        tasks={[]}
        selectedProjectId={null}
        onSelectProject={vi.fn()}
        userRole="user"
      />,
    );

    expect(screen.queryByTitle('Add new project')).not.toBeInTheDocument();
  });
});
