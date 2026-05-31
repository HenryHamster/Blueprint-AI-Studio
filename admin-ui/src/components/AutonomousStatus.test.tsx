import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AutonomousStatus } from './AutonomousStatus';

const { apiFetchMock, authHeadersMock } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
  authHeadersMock: vi.fn(),
}));

vi.mock('../api', () => ({
  apiFetch: apiFetchMock,
  authHeaders: authHeadersMock,
  API_BASE_URL: 'http://test',
}));

vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  signOut: vi.fn(),
}));

function makeProject(overrides: {
  project_id?: string;
  project_name?: string;
  autonomous_enabled?: boolean;
  consecutive_failures?: number;
  last_research_at?: string | null;
  tasks_spawned_last_hour?: number;
  rate_limit?: number;
} = {}) {
  return {
    project_id: 'proj-1',
    project_name: 'Test Project',
    autonomous_enabled: false,
    consecutive_failures: 0,
    last_research_at: null,
    tasks_spawned_last_hour: 3,
    rate_limit: 10,
    ...overrides,
  };
}

function makeDebug(overrides: { spawn_blocked?: boolean; blocking_guards?: unknown[]; circuit_breaker?: unknown } = {}) {
  return {
    spawn_blocked: false,
    blocking_guards: [],
    circuit_breaker: {
      state: 'CLOSED',
      failure_count: 0,
      success_count: 0,
      opened_at: null,
      cooldown_seconds: 300,
      cooldown_ends_at: null,
    },
    rate_limit: { tasks_this_hour: 0, max_per_hour: 10 },
    directions: [],
    next_check_at: new Date(Date.now() + 30000).toISOString(),
    ...overrides,
  };
}

function jsonResponse(data: unknown): Response {
  return { ok: true, json: async () => data } as Response;
}

function statusResponse(projects: unknown[]): Response {
  return jsonResponse({ projects });
}

beforeEach(() => {
  authHeadersMock.mockResolvedValue({ Authorization: 'Bearer test' });
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(statusResponse([])));
  apiFetchMock.mockResolvedValue({ ok: false });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('AutonomousStatus badge', () => {
  it('shows Off when no projects are enabled', async () => {
    await act(async () => { render(<AutonomousStatus />); });
    expect(screen.getByText('Off')).toBeTruthy();
  });

  it('shows Active when a project is enabled without failures', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      statusResponse([makeProject({ autonomous_enabled: true, consecutive_failures: 0 })])
    ));
    await act(async () => { render(<AutonomousStatus />); });
    expect(screen.getByText('Active')).toBeTruthy();
  });

  it('shows Warn when an enabled project has circuit-breaker-level failures', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      statusResponse([makeProject({ autonomous_enabled: true, consecutive_failures: 2 })])
    ));
    await act(async () => { render(<AutonomousStatus />); });
    expect(screen.getByText('Warn')).toBeTruthy();
  });
});

describe('AutonomousStatus expanded dropdown', () => {
  async function renderExpanded(projects: unknown[], debugOverride = makeDebug()) {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(statusResponse(projects)));
    apiFetchMock.mockImplementation((path: string) => {
      if (path.includes('/autonomy-debug')) return Promise.resolve(jsonResponse(debugOverride));
      return Promise.resolve({ ok: false });
    });
    const user = userEvent.setup();
    await act(async () => { render(<AutonomousStatus />); });
    await user.click(screen.getByTitle('Autonomous Pipeline Status'));
    return user;
  }

  it('shows empty state when no projects exist', async () => {
    await renderExpanded([]);
    expect(screen.getByText('No projects found')).toBeTruthy();
  });

  it('shows pauseReason banner when prop is set', async () => {
    await renderExpanded([]);
    // Re-render with pauseReason — easiest to mount separately
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(statusResponse([])));
    const user = userEvent.setup();
    await act(async () => { render(<AutonomousStatus pauseReason="Manual pause by operator" />); });
    await user.click(screen.getAllByTitle('Autonomous Pipeline Status')[1]);
    expect(screen.getByText('Manual pause by operator')).toBeTruthy();
  });

  it('shows project name and toggle button', async () => {
    await renderExpanded([makeProject({ project_name: 'My Repo', autonomous_enabled: true })]);
    expect(screen.getByText('My Repo')).toBeTruthy();
    expect(screen.getByText('Enabled')).toBeTruthy();
  });

  it('shows Disabled button for a disabled project', async () => {
    await renderExpanded([makeProject({ autonomous_enabled: false })]);
    expect(screen.getByText('Disabled')).toBeTruthy();
  });

  it('shows rate limit usage counters', async () => {
    await renderExpanded([makeProject({ tasks_spawned_last_hour: 5, rate_limit: 10 })]);
    expect(screen.getByText('Tasks this hour')).toBeTruthy();
    expect(screen.getByText('/10')).toBeTruthy();
  });

  it('shows never for last research when null', async () => {
    await renderExpanded([makeProject({ last_research_at: null })]);
    expect(screen.getByText('never')).toBeTruthy();
  });

  it('shows Reset Circuit Breaker button when breaker is OPEN', async () => {
    const openDebug = makeDebug({
      circuit_breaker: {
        state: 'OPEN',
        failure_count: 3,
        success_count: 0,
        opened_at: new Date().toISOString(),
        cooldown_seconds: 300,
        cooldown_ends_at: new Date(Date.now() + 200000).toISOString(),
      },
    });
    await renderExpanded([makeProject({ autonomous_enabled: true })], openDebug);
    expect(screen.getByText('Reset Circuit Breaker')).toBeTruthy();
  });

  it('shows spawn-guard diagnostics when spawn is blocked', async () => {
    const blockedDebug = makeDebug({
      spawn_blocked: true,
      blocking_guards: [{ guard: 'rate_limit_exceeded', reason: 'Too many tasks', tasks_this_hour: 10, max_per_hour: 10 }],
    });
    await renderExpanded([makeProject({ autonomous_enabled: true })], blockedDebug);
    expect(screen.getByText("Why autonomy isn't spawning")).toBeTruthy();
    expect(screen.getByText(/Rate limit: 10\/10 tasks this hour/)).toBeTruthy();
  });
});
