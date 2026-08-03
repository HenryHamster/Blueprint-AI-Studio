import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import { ConsoleHome } from './ConsoleHome';
import type { Task } from '../types/task';

const { apiFetchMock, fetchFleetUtilizationMock } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
  fetchFleetUtilizationMock: vi.fn(),
}));

vi.mock('../api', () => ({
  apiFetch: (path: string, init?: RequestInit) => apiFetchMock(path, init),
  fetchFleetUtilization: () => fetchFleetUtilizationMock(),
  fetchTask: vi.fn(async () => ({})),
  triggerSelfAudit: vi.fn(async () => ({ task_id: 'audit-task' })),
}));

const TASKS: Task[] = [];

function digestResponse(overrides: Record<string, unknown>) {
  return new Response(
    JSON.stringify({
      failed_count: 0,
      pending_approval_count: 0,
      direction_progress: [],
      recent_completions: [],
      failure_groups: [],
      active_count: 0,
      running_count: 0,
      queued_count: 0,
      completed_count: 0,
      research_cycles: 0,
      prs_opened: 0,
      success_rate: 0.9,
      trend: 'stable',
      trend_history: [],
      ...overrides,
    }),
    { status: 200 },
  );
}

function renderHome(props: Partial<React.ComponentProps<typeof ConsoleHome>> = {}) {
  return render(
    <ConsoleHome
      selectedProjectId={null}
      scopedActiveCount={null}
      scopedQueuedCount={null}
      globalActiveCount={1}
      globalQueuedCount={2}
      tasks={TASKS}
      onViewTask={() => {}}
      onShowAnalytics={() => {}}
      isAdmin={true}
      {...props}
    />,
  );
}

describe('ConsoleHome — operator inbox', () => {
  beforeEach(() => {
    fetchFleetUtilizationMock.mockResolvedValue({ pools: [] });
    apiFetchMock.mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it('renders a failed Needs-Attention row with severity label, class chip, and inspect hint', async () => {
    apiFetchMock.mockImplementation(async (url: string) =>
      url.includes('/digest')
        ? digestResponse({
            needs_attention: [
              {
                task_id: 't-fail',
                title: 'fix: broken thing',
                reason: 'failed',
                failure_class: 'CODE_ERROR',
                scope: 'small',
                error_snippet: 'TypeError: x is undefined',
                pr_url: 'https://example/pr/1',
                waiting_hours: 3,
              },
            ],
          })
        : new Response('{}', { status: 200 }),
    );

    renderHome();

    await waitFor(() => expect(screen.getByText('Failed — needs investigation')).toBeTruthy());
    // failure_class chip (lower-cased) and scope chip
    expect(screen.getByText('code_error')).toBeTruthy();
    expect(screen.getByText('small')).toBeTruthy();
    // actionable "what to inspect next" hint mentions the PR diff
    expect(screen.getByText('Open to inspect the error and PR diff')).toBeTruthy();
  });

  it('labels an approval row as a decision with an approve/reject hint', async () => {
    apiFetchMock.mockImplementation(async (url: string) =>
      url.includes('/digest')
        ? digestResponse({
            needs_attention: [
              {
                task_id: 't-approve',
                title: 'compute_experiment: run benchmark',
                reason: 'approval_required',
                spend_cap_usd: 250,
                deadline_seconds: 86400,
                waiting_hours: 1,
              },
            ],
          })
        : new Response('{}', { status: 200 }),
    );

    renderHome();

    await waitFor(() => expect(screen.getByText('Awaiting your approval')).toBeTruthy());
    expect(screen.getByText('Open to approve, adjust the cap, or reject')).toBeTruthy();
  });

  it('shows an explicit "all clear" empty state once data has loaded', async () => {
    apiFetchMock.mockImplementation(async (url: string) =>
      url.includes('/digest') ? digestResponse({ needs_attention: [] }) : new Response('{}', { status: 200 }),
    );

    renderHome();

    await waitFor(() =>
      expect(screen.getByText(/Nothing needs your attention/)).toBeTruthy(),
    );
  });

  it('renders Recently Shipped with an unverified-merge badge', async () => {
    apiFetchMock.mockImplementation(async (url: string) =>
      url.includes('/digest')
        ? digestResponse({
            recent_completions: [
              {
                task_id: 't-done',
                title: 'feat: shipped feature',
                completed_at: new Date(Date.now() - 60_000).toISOString(),
                pr_url: 'https://example/pr/2',
                task_type: 'create',
                unverified_merge_reason: 'no_suite',
              },
            ],
          })
        : new Response('{}', { status: 200 }),
    );

    renderHome();

    await waitFor(() => expect(screen.getByText('Recently Shipped')).toBeTruthy());
    expect(screen.getByText('feat: shipped feature')).toBeTruthy();
    expect(screen.getByText('unverified')).toBeTruthy();
  });

  it('shows a stale-data banner when the digest is served from a last-good cache', async () => {
    apiFetchMock.mockImplementation(async (url: string) =>
      url.includes('/digest') ? digestResponse({ served_stale: true }) : new Response('{}', { status: 200 }),
    );

    renderHome();

    await waitFor(() =>
      expect(screen.getByText(/Live refresh failed/)).toBeTruthy(),
    );
  });

  it('shows an unreachable-orchestrator banner when the digest fetch fails', async () => {
    apiFetchMock.mockImplementation(async (url: string) => {
      if (url.includes('/digest')) throw new Error('network down');
      return new Response('{}', { status: 200 });
    });

    renderHome();

    await waitFor(() =>
      expect(screen.getByText(/Can't reach the orchestrator/)).toBeTruthy(),
    );
  });
});
