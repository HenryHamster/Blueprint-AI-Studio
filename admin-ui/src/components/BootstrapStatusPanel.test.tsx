import { describe, test, expect, vi, afterEach } from 'vitest';
import { render, waitFor, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { BootstrapStatusPanel } from './BootstrapStatusPanel';
import type { Project } from '../types/project';
import type { Task } from './TaskCard';
import type { BuildConfigCommandsResponse, BootstrapRetryResponse } from '../api';

const fetchBuildConfigCommands = vi.fn();
const retryProjectBootstrap = vi.fn();

vi.mock('../api', () => ({
  fetchBuildConfigCommands: (...args: unknown[]) => fetchBuildConfigCommands(...args),
  retryProjectBootstrap: (...args: unknown[]) => retryProjectBootstrap(...args),
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

  test('renders retry control when an external project has not bootstrapped yet', async () => {
    fetchBuildConfigCommands.mockResolvedValue({ project_id: 'proj-1', directories: [] });
    render(
      <BootstrapStatusPanel
        project={makeProject()}
        tasks={[]}
      />
    );

    expect(screen.getByText(/Bootstrap has not run yet/i)).toBeTruthy();
    expect(screen.getByRole('button', { name: /retry bootstrap/i })).toBeTruthy();
  });

  test('renders pending banner when repo_bootstrap task is IN_PROGRESS and hides retry', async () => {
    fetchBuildConfigCommands.mockResolvedValue({ project_id: 'proj-1', directories: [] });
    render(
      <BootstrapStatusPanel
        project={makeProject()}
        tasks={[makeTask({ status: 'IN_PROGRESS' })]}
      />
    );
    expect(screen.getByText(/Bootstrap pending/)).toBeTruthy();
    expect(screen.getByText(/implementation tasks deferred/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /retry bootstrap/i })).toBeNull();
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
    expect(screen.queryByRole('button', { name: /retry bootstrap/i })).toBeNull();
  });

  test('renders degraded warning and retries into pending state', async () => {
    const user = userEvent.setup();
    fetchBuildConfigCommands.mockResolvedValue({ project_id: 'proj-1', directories: [] });
    retryProjectBootstrap.mockResolvedValue({
      state: 'spawned',
      task_id: 'task-2',
      reason: 'operator_retry',
    } satisfies BootstrapRetryResponse);

    render(
      <BootstrapStatusPanel
        project={makeProject({
          build_config: { bootstrap_degraded_reason: 'bootstrap failed previously' },
        })}
        tasks={[]}
      />
    );

    expect(screen.getByText(/Bootstrap degraded/)).toBeTruthy();
    expect(screen.getByRole('button', { name: /retry bootstrap/i })).toBeTruthy();

    await user.click(screen.getByRole('button', { name: /retry bootstrap/i }));

    await waitFor(() => {
      expect(retryProjectBootstrap).toHaveBeenCalledWith('proj-1');
    });
    await waitFor(() => {
      expect(screen.getByText(/Bootstrap pending/)).toBeTruthy();
      expect(screen.getByText(/bootstrap is now running/i)).toBeTruthy();
      expect(screen.queryByRole('button', { name: /retry bootstrap/i })).toBeNull();
    });
  });

  test('surfaces already-active retry outcome as inline info', async () => {
    const user = userEvent.setup();
    fetchBuildConfigCommands.mockResolvedValue({ project_id: 'proj-1', directories: [] });
    retryProjectBootstrap.mockResolvedValue({
      state: 'already_active',
      task_id: 'task-9',
      reason: 'existing active bootstrap',
    } satisfies BootstrapRetryResponse);

    render(
      <BootstrapStatusPanel
        project={makeProject()}
        tasks={[]}
      />
    );

    await user.click(screen.getByRole('button', { name: /retry bootstrap/i }));

    await waitFor(() => {
      expect(retryProjectBootstrap).toHaveBeenCalledWith('proj-1');
    });
    await waitFor(() => {
      expect(screen.getByText(/already running \(task task-9\)/i)).toBeTruthy();
      expect(screen.queryByRole('button', { name: /retry bootstrap/i })).toBeNull();
    });
  });

  test('renders command audit table with stale badge on expand', async () => {
    const user = userEvent.setup();
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
    await user.click(expandBtn);
    expect(screen.getAllByText('npm test').length).toBeGreaterThan(0);
    expect(screen.getByText('stale')).toBeTruthy();
  });

  test('renders command audit table with confirmed badge', async () => {
    const user = userEvent.setup();
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
    await user.click(expandBtn);
    expect(screen.getAllByText('npm test').length).toBeGreaterThan(0);
    expect(screen.getByText('confirmed')).toBeTruthy();
  });

  test('does not render retry control when bootstrap task is active', () => {
    fetchBuildConfigCommands.mockResolvedValue({ project_id: 'proj-1', directories: [] });
    render(
      <BootstrapStatusPanel
        project={makeProject()}
        tasks={[makeTask({ status: 'QUEUED' })]}
      />
    );
    expect(screen.getByText(/Bootstrap pending/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /retry bootstrap/i })).toBeNull();
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

  test('renders degraded warning from bootstrap_degraded_reason', async () => {
    fetchBuildConfigCommands.mockResolvedValue({ project_id: 'proj-1', directories: [] });
    render(
      <BootstrapStatusPanel
        project={makeProject({
          build_config: { bootstrap_degraded_reason: 'design doc section write failed' },
        })}
        tasks={[]}
      />
    );
    expect(screen.getByText(/Bootstrap degraded/)).toBeTruthy();
    expect(screen.getByText(/design doc section write failed/)).toBeTruthy();
  });

  test('shows completed (not degraded) when bootstrap_completed_at is set even if bootstrap_degraded_reason is present', async () => {
    fetchBuildConfigCommands.mockResolvedValue({ project_id: 'proj-1', directories: [] });
    render(
      <BootstrapStatusPanel
        project={makeProject({
          build_config: {
            bootstrap_completed_at: '2026-06-01T12:00:00Z',
            bootstrap_degraded_reason: 'some partial failure',
          },
        })}
        tasks={[]}
      />
    );
    expect(screen.getByText(/Completed/)).toBeTruthy();
    expect(screen.queryByText(/Bootstrap degraded/)).toBeNull();
  });

  test('shows degraded when bootstrap task is COMPLETED but bootstrap_degraded_reason is set without bootstrap_completed_at', async () => {
    fetchBuildConfigCommands.mockResolvedValue({ project_id: 'proj-1', directories: [] });
    render(
      <BootstrapStatusPanel
        project={makeProject({
          build_config: { bootstrap_degraded_reason: 'task completed without writing design doc' },
        })}
        tasks={[makeTask({ status: 'COMPLETED' })]}
      />
    );
    expect(screen.getByText(/Bootstrap degraded/)).toBeTruthy();
    expect(screen.getByText(/task completed without writing design doc/)).toBeTruthy();
  });

  test('shows degraded without reason text when bootstrap task FAILED and no bootstrap_degraded_reason', async () => {
    fetchBuildConfigCommands.mockResolvedValue({ project_id: 'proj-1', directories: [] });
    render(
      <BootstrapStatusPanel
        project={makeProject()}
        tasks={[makeTask({ status: 'FAILED' })]}
      />
    );
    expect(screen.getByText(/Bootstrap failed/)).toBeTruthy();
    expect(screen.getByText(/cycles running without repo memory/)).toBeTruthy();
    expect(screen.queryByText(/Bootstrap degraded/)).toBeNull();
  });
});
