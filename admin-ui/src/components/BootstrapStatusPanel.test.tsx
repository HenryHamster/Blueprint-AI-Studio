import { describe, test, expect, vi, afterEach } from 'vitest';
import { render, waitFor, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { BootstrapStatusPanel } from './BootstrapStatusPanel';
import type { Project } from '../types/project';
import type { Task } from './TaskCard';
import type { BuildConfigCommandsResponse } from '../api';

const fetchBuildConfigCommands = vi.fn();

vi.mock('../api', () => ({
  fetchBuildConfigCommands: (...args: unknown[]) => fetchBuildConfigCommands(...args),
}));

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    id: 'proj-1',
    name: 'test-project',
    status: 'ACTIVE',
    github_owner: 'org',
    github_repo: 'repo',
    autonomous_enabled: true,
    created_at: '2026-01-01T00:00:00Z',
    build_config: {},
    ...overrides,
  };
}

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-1',
    title: 'repo_bootstrap',
    status: 'IN_PROGRESS',
    type: 'repo_bootstrap',
    created_at: '2026-01-01T00:00:00Z',
    payloads: [{ type: 'repo_bootstrap' }],
    ...overrides,
  } as unknown as Task;
}

function makeCommandsResponse(stale = false): BuildConfigCommandsResponse {
  return {
    project_id: 'proj-1',
    directories: [
      {
        path: '.',
        type: 'node',
        detected_test_cmd: 'npm test',
        detected_build_cmd: 'npm run build',
        detected_install_cmd: 'npm install',
        confirmed_test_cmd: stale ? 'npm test' : null,
        confirmed_build_cmd: null,
        confirmed_install_cmd: null,
        commands_confirmed_at: stale ? '2026-01-01T00:00:00Z' : null,
        confirming_task_id: stale ? 'task-1' : null,
        commands_stale: stale,
      },
    ],
  };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe('BootstrapStatusPanel', () => {
  test('renders nothing for self_repo projects', () => {
    fetchBuildConfigCommands.mockResolvedValue({ project_id: 'proj-1', directories: [] });
    const { container } = render(
      <BootstrapStatusPanel
        project={makeProject({ is_self_repo: true })}
        tasks={[makeTask()]}
      />
    );
    expect(container.firstChild).toBeNull();
  });

  test('renders nothing when no bootstrap task and no bootstrap_completed_at', () => {
    fetchBuildConfigCommands.mockResolvedValue({ project_id: 'proj-1', directories: [] });
    const { container } = render(
      <BootstrapStatusPanel
        project={makeProject()}
        tasks={[]}
      />
    );
    expect(container.firstChild).toBeNull();
  });

  test('renders pending banner when repo_bootstrap task is IN_PROGRESS', async () => {
    fetchBuildConfigCommands.mockResolvedValue({ project_id: 'proj-1', directories: [] });
    render(
      <BootstrapStatusPanel
        project={makeProject()}
        tasks={[makeTask({ status: 'IN_PROGRESS' })]}
      />
    );
    expect(screen.getByText(/Bootstrap pending/)).toBeTruthy();
    expect(screen.getByText(/implementation tasks deferred/)).toBeTruthy();
  });

  test('renders pending banner when repo_bootstrap task is QUEUED', async () => {
    fetchBuildConfigCommands.mockResolvedValue({ project_id: 'proj-1', directories: [] });
    render(
      <BootstrapStatusPanel
        project={makeProject()}
        tasks={[makeTask({ status: 'QUEUED' })]}
      />
    );
    expect(screen.getByText(/Bootstrap pending/)).toBeTruthy();
  });

  test('renders completed state with timestamp when bootstrap_completed_at is set', async () => {
    fetchBuildConfigCommands.mockResolvedValue({ project_id: 'proj-1', directories: [] });
    render(
      <BootstrapStatusPanel
        project={makeProject({ build_config: { bootstrap_completed_at: '2026-06-01T12:00:00Z' } })}
        tasks={[]}
      />
    );
    expect(screen.getByText(/Completed/)).toBeTruthy();
  });

  test('renders degraded warning when bootstrap task FAILED', async () => {
    fetchBuildConfigCommands.mockResolvedValue({ project_id: 'proj-1', directories: [] });
    render(
      <BootstrapStatusPanel
        project={makeProject()}
        tasks={[makeTask({ status: 'FAILED' })]}
      />
    );
    expect(screen.getByText(/Bootstrap failed/)).toBeTruthy();
    expect(screen.getByText(/cycles running without repo memory/)).toBeTruthy();
  });

  test('renders degraded warning when bootstrap task CANCELLED', async () => {
    fetchBuildConfigCommands.mockResolvedValue({ project_id: 'proj-1', directories: [] });
    render(
      <BootstrapStatusPanel
        project={makeProject()}
        tasks={[makeTask({ status: 'CANCELLED' })]}
      />
    );
    expect(screen.getByText(/Bootstrap failed/)).toBeTruthy();
  });

  test('renders command audit table with stale badge on expand', async () => {
    fetchBuildConfigCommands.mockResolvedValue(makeCommandsResponse(true));
    render(
      <BootstrapStatusPanel
        project={makeProject({ build_config: { bootstrap_completed_at: '2026-06-01T12:00:00Z' } })}
        tasks={[]}
      />
    );
    await waitFor(() => {
      expect(fetchBuildConfigCommands).toHaveBeenCalledWith('proj-1', expect.any(AbortSignal));
    });
    const expandBtn = screen.getByText(/Commands/);
    await userEvent.click(expandBtn);
    expect(screen.getAllByText('npm test').length).toBeGreaterThan(0);
    expect(screen.getByText('stale')).toBeTruthy();
  });

  test('renders command audit table with confirmed badge', async () => {
    const response = makeCommandsResponse(false);
    response.directories[0].confirmed_test_cmd = 'npm test';
    response.directories[0].commands_confirmed_at = '2026-06-01T10:00:00Z';
    fetchBuildConfigCommands.mockResolvedValue(response);
    render(
      <BootstrapStatusPanel
        project={makeProject({ build_config: { bootstrap_completed_at: '2026-06-01T12:00:00Z' } })}
        tasks={[]}
      />
    );
    await waitFor(() => {
      expect(fetchBuildConfigCommands).toHaveBeenCalled();
    });
    const expandBtn = screen.getByText(/Commands/);
    await userEvent.click(expandBtn);
    expect(screen.getAllByText('npm test').length).toBeGreaterThan(0);
    expect(screen.getByText('confirmed')).toBeTruthy();
  });

  test('does not call fetchBuildConfigCommands when state is none', () => {
    render(
      <BootstrapStatusPanel
        project={makeProject({ is_self_repo: true })}
        tasks={[]}
      />
    );
    expect(fetchBuildConfigCommands).not.toHaveBeenCalled();
  });
});
