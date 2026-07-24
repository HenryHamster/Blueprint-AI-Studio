import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

// firebase.ts calls getAuth() at module load; stub it out so tests don't need env vars.
// PRMonitorSection imports isSafeUrl from ./TaskCard, which transitively imports ../api -> ../firebase.
vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  getIdToken: vi.fn().mockResolvedValue('test-token'),
  getIdTokenAfterAuthRestore: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
  signOut: vi.fn(),
}));

import { PRMonitorSection } from './PRMonitorSection';
import type { OpenPR, OpenPRTask } from './PRMonitorSection';
import type { Task } from '../types/task';

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-1',
    title: 'Implement feature',
    description: 'desc',
    status: 'IN_PROGRESS',
    created_by: 'agent',
    requires_approval: false,
    created_at: '2026-04-28T00:00:00Z',
    ...overrides,
  };
}

function makeOpenPRTask(overrides: Partial<OpenPRTask> = {}): OpenPRTask {
  return {
    task_id: 'task-1',
    task_status: 'IN_PROGRESS',
    task_type: 'fix',
    task_title: 'Fix the thing',
    ...overrides,
  };
}

function makeOpenPR(overrides: Partial<OpenPR> = {}): OpenPR {
  return {
    pr_number: 123,
    pr_url: 'https://github.com/acme/repo/pull/123',
    pr_title: 'Add retry logic',
    pr_branch: 'feature/retry-logic',
    pr_created_at: '2026-04-29T00:00:00Z',
    tasks: [],
    is_orphaned: false,
    ...overrides,
  };
}

describe('PRMonitorSection', () => {
  const baseProps = {
    openPRs: [] as OpenPR[],
    loading: false,
    refreshing: false,
    onRefresh: vi.fn(),
    hasProject: true,
    tasks: [] as Task[],
    onViewTask: vi.fn(),
  };

  it('renders "Select a project" and no refresh button when hasProject is false', () => {
    render(<PRMonitorSection {...baseProps} hasProject={false} />);
    expect(screen.getByText('Select a project')).toBeInTheDocument();
    // The chevron header button is present, but no refresh button.
    // RefreshCw renders no accessible name; assert by counting buttons.
    expect(screen.getAllByRole('button')).toHaveLength(1);
  });

  it('renders skeleton rows when loading is true and openPRs is empty', () => {
    const { container } = render(
      <PRMonitorSection {...baseProps} loading={true} openPRs={[]} />,
    );
    const skeletons = container.querySelectorAll('.animate-pulse');
    expect(skeletons.length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText('No open PRs')).toBeNull();
    expect(screen.queryByText('Select a project')).toBeNull();
  });

  it('renders "No open PRs" when hasProject=true, not loading, and openPRs is empty', () => {
    render(<PRMonitorSection {...baseProps} loading={false} openPRs={[]} />);
    expect(screen.getByText('No open PRs')).toBeInTheDocument();
    expect(screen.queryByTestId('open-prs-stale-badge')).toBeNull();
  });

  it('renders an outage badge when the empty response is degraded', () => {
    render(
      <PRMonitorSection
        {...baseProps}
        loading={false}
        openPRs={[]}
        degraded={true}
      />,
    );
    expect(screen.getByTestId('open-prs-stale-badge')).toHaveTextContent('GitHub unreachable');
    expect(screen.getAllByText('GitHub unreachable')).toHaveLength(2);
  });

  it('renders an orphaned PR with "No active task" and a red dot indicator', () => {
    const pr = makeOpenPR({ pr_number: 7, is_orphaned: true, tasks: [] });
    const { container } = render(
      <PRMonitorSection {...baseProps} openPRs={[pr]} />,
    );
    expect(screen.getByText('No active task')).toBeInTheDocument();
    // Red dot indicator
    expect(container.querySelector('.bg-red-400')).not.toBeNull();
    expect(container.querySelector('.bg-emerald-400')).toBeNull();
  });

  it('renders header with pr_number, pr_title, and pr_branch', () => {
    const pr = makeOpenPR({
      pr_number: 999,
      pr_title: 'Fix flaky migration test',
      pr_branch: 'fix/flaky-migration',
      tasks: [makeOpenPRTask()],
    });
    render(<PRMonitorSection {...baseProps} openPRs={[pr]} />);
    expect(screen.getByText('#999')).toBeInTheDocument();
    expect(screen.getByText('Fix flaky migration test')).toBeInTheDocument();
    expect(screen.getByText('fix/flaky-migration')).toBeInTheDocument();
  });

  it('renders head→base branch format when pr_base_branch is set', () => {
    const pr = makeOpenPR({
      pr_branch: 'feature/my-change',
      pr_base_branch: 'develop',
    });
    render(<PRMonitorSection {...baseProps} openPRs={[pr]} />);
    expect(screen.getByText('feature/my-change')).toBeInTheDocument();
    expect(screen.getByText('develop')).toBeInTheDocument();
    expect(screen.getByText('→')).toBeInTheDocument();
  });

  it('renders drift chip when pr_base_branch differs from integrationBranch', () => {
    const pr = makeOpenPR({
      pr_branch: 'feature/my-change',
      pr_base_branch: 'some-other-branch',
    });
    render(
      <PRMonitorSection
        {...baseProps}
        openPRs={[pr]}
        integrationBranch="develop"
        promotionTarget="main"
      />,
    );
    expect(screen.getByText('drift')).toBeInTheDocument();
  });

  it('does not render drift chip when pr_base_branch matches integrationBranch', () => {
    const pr = makeOpenPR({
      pr_branch: 'feature/my-change',
      pr_base_branch: 'develop',
    });
    render(
      <PRMonitorSection
        {...baseProps}
        openPRs={[pr]}
        integrationBranch="develop"
        promotionTarget="main"
      />,
    );
    expect(screen.queryByText('drift')).toBeNull();
  });

  it('does not render drift chip when pr_base_branch matches promotionTarget', () => {
    const pr = makeOpenPR({
      pr_branch: 'feature/my-change',
      pr_base_branch: 'main',
    });
    render(
      <PRMonitorSection
        {...baseProps}
        openPRs={[pr]}
        integrationBranch="develop"
        promotionTarget="main"
      />,
    );
    expect(screen.queryByText('drift')).toBeNull();
  });

  it('renders a clickable button for a linked PR whose task is in the tasks prop', async () => {
    const user = userEvent.setup();
    const fullTask = makeTask({ id: 'task-42', title: 'Real task object' });
    const otherTask = makeTask({ id: 'task-other', title: 'Some other task' });
    const pr = makeOpenPR({
      tasks: [makeOpenPRTask({ task_id: 'task-42', task_title: 'PR-shape title' })],
    });
    const onViewTask = vi.fn();

    render(
      <PRMonitorSection
        {...baseProps}
        openPRs={[pr]}
        tasks={[otherTask, fullTask]}
        onViewTask={onViewTask}
      />,
    );

    // The row text shows the OpenPRTask.task_title (PR-shape title).
    const row = screen.getByText('PR-shape title').closest('button');
    expect(row).not.toBeNull();
    if (!row) throw new Error('test setup: expected button row');
    await user.click(row);

    // onViewTask is called with the FULL Task object, not the OpenPRTask.
    expect(onViewTask).toHaveBeenCalledTimes(1);
    expect(onViewTask).toHaveBeenCalledWith(fullTask);
  });

  it('renders a non-clickable row when the linked task is not in the tasks prop', async () => {
    const user = userEvent.setup();
    const onViewTask = vi.fn();
    const pr = makeOpenPR({
      tasks: [makeOpenPRTask({ task_id: 'missing-id', task_title: 'Stale task' })],
    });

    render(
      <PRMonitorSection
        {...baseProps}
        openPRs={[pr]}
        tasks={[]}
        onViewTask={onViewTask}
      />,
    );

    // The text exists, but it should not be inside a <button>.
    const text = screen.getByText('Stale task');
    expect(text.closest('button')).toBeNull();

    // Clicking the row's surrounding div does not invoke onViewTask.
    await user.click(text);
    expect(onViewTask).not.toHaveBeenCalled();
  });

  it('clicking refresh calls onRefresh', async () => {
    const user = userEvent.setup();
    const onRefresh = vi.fn();
    render(<PRMonitorSection {...baseProps} onRefresh={onRefresh} />);
    // The refresh button has title "Refresh open PRs" when not refreshing.
    const refresh = screen.getByTitle('Refresh open PRs');
    await user.click(refresh);
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it('refresh button is disabled and spinner spins while loading', () => {
    render(<PRMonitorSection {...baseProps} loading={true} />);
    const refresh = screen.getByTitle('Refresh open PRs');
    expect(refresh).toBeDisabled();
    // Spinner icon has the animate-spin class while busy.
    expect(refresh.querySelector('.animate-spin')).not.toBeNull();
  });

  it('refresh button is disabled and shows refreshing title while refreshing', () => {
    render(<PRMonitorSection {...baseProps} refreshing={true} />);
    const refresh = screen.getByTitle('Refreshing…');
    expect(refresh).toBeDisabled();
    expect(refresh.querySelector('.animate-spin')).not.toBeNull();
  });

  it('expand/collapse toggle hides the body when collapsed (default expanded)', async () => {
    const user = userEvent.setup();
    const pr = makeOpenPR({ pr_title: 'Toggle target PR' });
    render(<PRMonitorSection {...baseProps} openPRs={[pr]} />);

    // Default: expanded — the PR title is visible.
    expect(screen.getByText('Toggle target PR')).toBeInTheDocument();

    // Click the header chevron-button. Both this button and the refresh
    // button match /Open PRs/i (refresh has title="Refresh open PRs"),
    // so locate it via the h2 it wraps.
    const header = screen.getByRole('heading', { name: 'Open PRs' }).closest('button');
    if (!header) throw new Error('test setup: expected header button');
    await user.click(header);

    // After collapse, the PR row is no longer rendered.
    expect(screen.queryByText('Toggle target PR')).toBeNull();

    // Re-expand brings it back.
    await user.click(header);
    expect(screen.getByText('Toggle target PR')).toBeInTheDocument();
  });

  it('renders a safe https pr_url as an external anchor (target=_blank, rel includes noopener noreferrer)', () => {
    const pr = makeOpenPR({ pr_url: 'https://github.com/acme/repo/pull/77' });
    const { container } = render(
      <PRMonitorSection {...baseProps} openPRs={[pr]} />,
    );
    const anchor = container.querySelector('a[href="https://github.com/acme/repo/pull/77"]');
    expect(anchor).not.toBeNull();
    if (!anchor) throw new Error('test setup: expected anchor element');
    expect(anchor.getAttribute('target')).toBe('_blank');
    const rel = anchor.getAttribute('rel') ?? '';
    expect(rel).toContain('noopener');
    expect(rel).toContain('noreferrer');
  });

  it('renders Spawn and Dismiss icon buttons only on orphaned rows', () => {
    const orphan = makeOpenPR({ pr_number: 10, is_orphaned: true, tasks: [] });
    const linked = makeOpenPR({
      pr_number: 11,
      is_orphaned: false,
      tasks: [makeOpenPRTask({ task_id: 'task-x' })],
    });
    render(
      <PRMonitorSection
        {...baseProps}
        openPRs={[orphan, linked]}
        onSpawnFixTask={vi.fn()}
      />,
    );
    // Each orphan row contributes one Spawn + one Dismiss button.
    expect(screen.getAllByTitle('Spawn fix task')).toHaveLength(1);
    expect(screen.getAllByTitle('Dismiss until refresh')).toHaveLength(1);
  });

  it('hides the Spawn button when onSpawnFixTask is omitted but still shows Dismiss', () => {
    const orphan = makeOpenPR({ pr_number: 13, is_orphaned: true, tasks: [] });
    render(<PRMonitorSection {...baseProps} openPRs={[orphan]} />);
    expect(screen.queryByTitle('Spawn fix task')).toBeNull();
    expect(screen.getByTitle('Dismiss until refresh')).toBeInTheDocument();
  });

  it('clicking Spawn invokes onSpawnFixTask with the full PR object', async () => {
    const user = userEvent.setup();
    const orphan = makeOpenPR({
      pr_number: 42,
      pr_title: 'Stuck orphan PR',
      pr_url: 'https://github.com/acme/repo/pull/42',
      pr_branch: 'fix/stuck',
      is_orphaned: true,
      tasks: [],
    });
    const onSpawnFixTask = vi.fn();
    render(
      <PRMonitorSection
        {...baseProps}
        openPRs={[orphan]}
        onSpawnFixTask={onSpawnFixTask}
      />,
    );
    await user.click(screen.getByTitle('Spawn fix task'));
    expect(onSpawnFixTask).toHaveBeenCalledTimes(1);
    expect(onSpawnFixTask).toHaveBeenCalledWith(orphan);
  });

  it('clicking Dismiss hides only that PR row and leaves the other rendered', async () => {
    const user = userEvent.setup();
    const a = makeOpenPR({ pr_number: 100, pr_title: 'Orphan A', is_orphaned: true, tasks: [] });
    const b = makeOpenPR({ pr_number: 101, pr_title: 'Orphan B', is_orphaned: true, tasks: [] });
    render(<PRMonitorSection {...baseProps} openPRs={[a, b]} />);
    expect(screen.getByText('Orphan A')).toBeInTheDocument();
    expect(screen.getByText('Orphan B')).toBeInTheDocument();

    const dismissButtons = screen.getAllByTitle('Dismiss until refresh');
    await user.click(dismissButtons[0]);

    expect(screen.queryByText('Orphan A')).toBeNull();
    expect(screen.getByText('Orphan B')).toBeInTheDocument();
  });

  it('dismiss state persists across parent re-renders with the same openPRs (only remount resets it)', async () => {
    const user = userEvent.setup();
    const a = makeOpenPR({ pr_number: 200, pr_title: 'Orphan persist A', is_orphaned: true, tasks: [] });
    const b = makeOpenPR({ pr_number: 201, pr_title: 'Orphan persist B', is_orphaned: true, tasks: [] });
    const { rerender, unmount } = render(<PRMonitorSection {...baseProps} openPRs={[a, b]} />);

    await user.click(screen.getAllByTitle('Dismiss until refresh')[0]);
    expect(screen.queryByText('Orphan persist A')).toBeNull();

    // Re-render with the same openPRs list — dismiss state must NOT reset.
    rerender(<PRMonitorSection {...baseProps} openPRs={[a, b]} />);
    expect(screen.queryByText('Orphan persist A')).toBeNull();
    expect(screen.getByText('Orphan persist B')).toBeInTheDocument();

    // Real remount (simulating a page reload) restores the dismissed row.
    unmount();
    render(<PRMonitorSection {...baseProps} openPRs={[a, b]} />);
    expect(screen.getByText('Orphan persist A')).toBeInTheDocument();
    expect(screen.getByText('Orphan persist B')).toBeInTheDocument();
  });

  it('header count reflects visible (non-dismissed) PR count', async () => {
    const user = userEvent.setup();
    const a = makeOpenPR({ pr_number: 300, pr_title: 'Count A', is_orphaned: true, tasks: [] });
    const b = makeOpenPR({ pr_number: 301, pr_title: 'Count B', is_orphaned: true, tasks: [] });
    const { container } = render(<PRMonitorSection {...baseProps} openPRs={[a, b]} />);
    const header = screen.getByRole('heading', { name: 'Open PRs' });
    // Count badge sits adjacent to the heading.
    const countSpan = header.parentElement?.querySelector('span.font-mono');
    expect(countSpan?.textContent).toBe('2');

    await user.click(screen.getAllByTitle('Dismiss until refresh')[0]);
    expect(countSpan?.textContent).toBe('1');
    expect(container.querySelector('span.font-mono')?.textContent).toBe('1');
  });

  it('does not render an anchor for an unsafe javascript: pr_url', () => {
    const pr = makeOpenPR({ pr_url: 'javascript:alert(1)' });
    const { container } = render(
      <PRMonitorSection {...baseProps} openPRs={[pr]} />,
    );
    // No anchor in the rendered output.
    expect(container.querySelector('a')).toBeNull();
  });
});
