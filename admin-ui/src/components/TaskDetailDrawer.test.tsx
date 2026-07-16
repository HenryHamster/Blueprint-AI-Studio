import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const apiMocks = vi.hoisted(() => ({
  authGet: vi.fn(),
  fetchTask: vi.fn(),
  fetchTaskPrFiles: vi.fn(),
}));

// Stub firebase before any module that transitively imports it — getAuth()
// throws at load time when test env lacks VITE_FIREBASE_* vars.
vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
  getIdTokenAfterAuthRestore: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
  signOut: vi.fn(),
}));

vi.mock('../api', () => ({
  API_BASE_URL: '',
  authGet: apiMocks.authGet,
  fetchTask: apiMocks.fetchTask,
  fetchTaskPrFiles: apiMocks.fetchTaskPrFiles,
}));

import { TaskDetailDrawer } from './TaskDetailDrawer';
import type { Task } from '../types/task';

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-1',
    title: 'Test Task',
    description: 'A test description',
    status: 'IN_PROGRESS',
    task_type: 'create',
    created_at: '2024-01-01T00:00:00Z',
    created_by: 'USER',
    requires_approval: false,
    ...overrides,
  };
}

const baseHandlers = {
  onClose: vi.fn(),
  onApprove: vi.fn(),
  onRequestChanges: vi.fn(),
  onCancel: vi.fn(),
  onRetry: vi.fn(),
  onEditPrompt: vi.fn(),
  onDismiss: vi.fn(),
};

let writeTextSpy: ReturnType<typeof vi.fn>;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

function makeCancelImpact(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    task_id: 'task-1',
    status: 'IN_PROGRESS',
    waiting_dependent_count: 0,
    waiting_dependent_statuses: ['QUEUED', 'BLOCKED', 'PROVISIONING', 'PARKED'],
    active_child_cascade_count: 0,
    active_child_cascade_statuses: ['QUEUED', 'PROVISIONING', 'IN_PROGRESS', 'EVALUATING'],
    target_vm_instance_id: null,
    target_has_vm_id: false,
    target_vm_provisioned: false,
    active_child_vm_id_count: 0,
    active_child_vm_provisioned_count: 0,
    orphan_pr_cleanup_context_reference_count: 0,
    pull_request_artifact_task_count: 0,
    ...overrides,
  };
}

describe('TaskDetailDrawer', () => {
  const originalError = console.error;

  beforeEach(() => {
    console.error = vi.fn();
    // scrollIntoView is not implemented in jsdom
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
    writeTextSpy = vi.fn().mockResolvedValue(undefined);
    apiMocks.authGet.mockResolvedValue(jsonResponse(makeCancelImpact()));
    apiMocks.fetchTask.mockResolvedValue(null);
    apiMocks.fetchTaskPrFiles.mockResolvedValue({ files: null, pr_number: null });
  });

  afterEach(() => {
    console.error = originalError;
    vi.clearAllMocks();
  });

  it('renders task title in the details tab', () => {
    render(<TaskDetailDrawer task={makeTask()} logLines={[]} {...baseHandlers} />);
    expect(screen.getByText('Test Task')).toBeInTheDocument();
  });

  it('renders task description in the details tab', () => {
    render(
      <TaskDetailDrawer
        task={makeTask({ description: 'My specific description' })}
        logLines={[]}
        {...baseHandlers}
      />
    );
    expect(screen.getByText('My specific description')).toBeInTheDocument();
  });

  it('renders task type badge', () => {
    render(
      <TaskDetailDrawer
        task={makeTask({ task_type: 'research' })}
        logLines={[]}
        {...baseHandlers}
      />
    );
    // TASK_TYPE_STYLES['research'].label === 'Research'
    expect(screen.getByText('Research')).toBeInTheDocument();
  });

  it('clicking Logs tab shows log lines', async () => {
    const user = userEvent.setup();
    render(
      <TaskDetailDrawer
        task={makeTask()}
        logLines={['Line alpha', 'Line beta']}
        {...baseHandlers}
      />
    );
    await user.click(screen.getByText('Logs'));
    expect(screen.getAllByText(/Line alpha/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Line beta/).length).toBeGreaterThan(0);
  });

  it('log text filter removes non-matching lines from the DOM', async () => {
    const user = userEvent.setup();
    render(
      <TaskDetailDrawer
        task={makeTask()}
        logLines={['matching line', 'unrelated log']}
        {...baseHandlers}
      />
    );
    await user.click(screen.getByText('Logs'));
    await user.type(screen.getByPlaceholderText('Filter logs...'), 'matching');
    // Non-matching line is removed when filter is active
    expect(screen.queryByText('unrelated log')).not.toBeInTheDocument();
    // Count indicator confirms the filter reduced the visible lines
    expect(screen.getByText('1 / 2 lines')).toBeInTheDocument();
  });

  it('log level ERROR filter hides WARN lines', async () => {
    const user = userEvent.setup();
    render(
      <TaskDetailDrawer
        task={makeTask()}
        logLines={['[warn] a warning message', '[error] bad thing happened']}
        {...baseHandlers}
      />
    );
    await user.click(screen.getByText('Logs'));
    // ERROR button text includes the count
    await user.click(screen.getByText('ERROR (1)'));
    // WARN line is removed when ERROR level filter is active
    expect(screen.queryByText('[warn] a warning message')).not.toBeInTheDocument();
    // Error line remains visible
    expect(screen.queryAllByText(/bad thing happened/).length).toBeGreaterThan(0);
  });

  it('shows Approve button when requires_approval=true and status is active', () => {
    render(
      <TaskDetailDrawer
        task={makeTask({ requires_approval: true, status: 'EVALUATING' })}
        logLines={[]}
        {...baseHandlers}
      />
    );
    expect(screen.getByText('Approve & Merge')).toBeInTheDocument();
  });

  it('shows spend-approval wording for a compute_experiment approval', () => {
    render(
      <TaskDetailDrawer
        task={makeTask({
          requires_approval: true,
          status: 'EVALUATING',
          task_type: 'compute_experiment',
          payloads: [{ type: 'compute_experiment', spend_cap_usd: 12.5 }],
        })}
        logLines={[]}
        {...baseHandlers}
      />
    );
    expect(screen.getByText('Approve Spend ($12.50)')).toBeInTheDocument();
    expect(screen.queryByText('Approve & Merge')).not.toBeInTheDocument();
  });

  it('shows "Approve Spend" without a cap when compute_experiment has no spend_cap_usd', () => {
    render(
      <TaskDetailDrawer
        task={makeTask({
          requires_approval: true,
          status: 'EVALUATING',
          task_type: 'compute_experiment',
          payloads: [{ type: 'compute_experiment' }],
        })}
        logLines={[]}
        {...baseHandlers}
      />
    );
    expect(screen.getByText('Approve Spend')).toBeInTheDocument();
  });

  it('cancel confirmation spells out the running consequence (VM cleanup, work discarded)', async () => {
    const user = userEvent.setup();
    render(
      <TaskDetailDrawer
        task={makeTask({ status: 'IN_PROGRESS' })}
        logLines={[]}
        {...baseHandlers}
      />
    );
    await user.click(screen.getByText('Cancel Task'));
    expect(screen.getByText(/cleans up its VM/i)).toBeInTheDocument();
    expect(screen.getByText(/In-progress work is discarded/i)).toBeInTheDocument();
    // No stranded-dependents warning when nothing depends on this task.
    expect(screen.queryByTestId('cancel-blast-radius')).not.toBeInTheDocument();
  });

  it('cancel confirmation warns about stranded waiting dependents (blast radius)', async () => {
    const user = userEvent.setup();
    apiMocks.authGet.mockRejectedValueOnce(new Error('impact endpoint unavailable'));
    const parent = makeTask({ id: 'parent', status: 'IN_PROGRESS' });
    const dependents = [
      makeTask({ id: 'child-1', status: 'QUEUED', depends_on: ['parent'] }),
      makeTask({ id: 'child-2', status: 'BLOCKED', depends_on: ['parent'] }),
      makeTask({ id: 'child-3', status: 'PARKED', depends_on: ['parent'] }),
      // A completed dependent must NOT count toward the blast radius.
      makeTask({ id: 'child-4', status: 'COMPLETED', depends_on: ['parent'] }),
    ];
    render(
      <TaskDetailDrawer
        task={parent}
        tasks={[parent, ...dependents]}
        logLines={[]}
        {...baseHandlers}
      />
    );
    await user.click(screen.getByText('Cancel Task'));
    expect(await screen.findByTestId('cancel-impact-fallback')).toHaveTextContent(
      'Full impact check failed',
    );
    const blast = await screen.findByTestId('cancel-blast-radius');
    expect(blast).toHaveTextContent('3 visible waiting tasks depend on this in the loaded task set');
  });

  it('blocks confirm cancel until backend cancel-impact has resolved', async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    let resolveImpact: (response: Response) => void = () => {};
    apiMocks.authGet.mockReturnValueOnce(
      new Promise<Response>(resolve => {
        resolveImpact = resolve;
      }),
    );

    render(
      <TaskDetailDrawer
        task={makeTask({ status: 'IN_PROGRESS' })}
        logLines={[]}
        {...baseHandlers}
        onCancel={onCancel}
      />,
    );

    await user.click(screen.getByText('Cancel Task'));
    const confirmButton = screen.getByRole('button', { name: /Confirm cancel/i });
    expect(confirmButton).toBeDisabled();

    await user.click(confirmButton);
    expect(onCancel).not.toHaveBeenCalled();

    await act(async () => {
      resolveImpact(jsonResponse(makeCancelImpact()));
    });

    await waitFor(() => expect(confirmButton).not.toBeDisabled());
    await user.click(confirmButton);
    expect(onCancel).toHaveBeenCalledWith('task-1');
  });

  it('cancel confirmation uses backend cancel-impact count when available', async () => {
    const user = userEvent.setup();
    apiMocks.authGet.mockResolvedValueOnce(jsonResponse(makeCancelImpact({
      waiting_dependent_count: 4,
      active_child_cascade_count: 2,
      target_vm_instance_id: 'agent-vm-parent',
      target_has_vm_id: true,
      target_vm_provisioned: true,
      active_child_vm_id_count: 2,
      active_child_vm_provisioned_count: 1,
      orphan_pr_cleanup_context_reference_count: 2,
      pull_request_artifact_task_count: 1,
    })));
    const parent = makeTask({ id: 'parent', status: 'IN_PROGRESS' });
    const visibleDependent = makeTask({ id: 'child-visible', status: 'QUEUED', depends_on: ['parent'] });
    render(
      <TaskDetailDrawer
        task={parent}
        tasks={[parent, visibleDependent]}
        logLines={[]}
        {...baseHandlers}
      />
    );
    await user.click(screen.getByText('Cancel Task'));
    expect(await screen.findByText('4 waiting tasks depend on this and may be stranded.')).toBeInTheDocument();
    expect(screen.getByTestId('cancel-child-cascade')).toHaveTextContent('2 active child tasks may be cancelled');
    expect(screen.getByTestId('cancel-vm-cleanup')).toHaveTextContent('target VM provisioned');
    expect(screen.getByTestId('cancel-vm-cleanup')).toHaveTextContent('2 child VMs recorded');
    expect(screen.getByTestId('cancel-vm-cleanup')).toHaveTextContent('1 child VM provisioned');
    expect(screen.getByTestId('cancel-pr-cleanup')).toHaveTextContent(
      '2 tasks with PR context and 1 PR artifact task',
    );
    expect(screen.queryByText(/visible waiting task/)).not.toBeInTheDocument();
  });

  it('shows Retry button when task status is FAILED', () => {
    render(
      <TaskDetailDrawer
        task={makeTask({ status: 'FAILED' })}
        logLines={[]}
        {...baseHandlers}
      />
    );
    expect(screen.getByText('Retry')).toBeInTheDocument();
  });

  it('shows the timeout subtype badge for a FAILED AGENT_TIMEOUT task', () => {
    render(
      <TaskDetailDrawer
        task={makeTask({ status: 'FAILED', failure_class: 'AGENT_TIMEOUT', context: { timeout_subtype: 'oom' } })}
        logLines={[]}
        {...baseHandlers}
      />
    );
    expect(screen.getByText('Subtype:')).toBeInTheDocument();
    expect(screen.getByText('OOM')).toBeInTheDocument();
  });

  it('shows the external-API subtype badge for a FAILED EXTERNAL_API task', () => {
    render(
      <TaskDetailDrawer
        task={makeTask({ status: 'FAILED', failure_class: 'EXTERNAL_API', context: { external_api_subtype: 'provider_auth' } })}
        logLines={[]}
        {...baseHandlers}
      />
    );
    expect(screen.getByText('provider auth')).toBeInTheDocument();
  });

  it('shows the rate-limit kind badge for a FAILED RATE_LIMIT task', () => {
    render(
      <TaskDetailDrawer
        task={makeTask({ status: 'FAILED', failure_class: 'RATE_LIMIT', context: { rate_limit_kind: 'quota_exhausted' } })}
        logLines={[]}
        {...baseHandlers}
      />
    );
    expect(screen.getByText('quota exhausted')).toBeInTheDocument();
  });

  it('falls back to the raw subtype value when there is no friendly label', () => {
    render(
      <TaskDetailDrawer
        task={makeTask({ status: 'FAILED', failure_class: 'AGENT_TIMEOUT', context: { timeout_subtype: 'brand_new_subtype' } })}
        logLines={[]}
        {...baseHandlers}
      />
    );
    expect(screen.getByText('brand_new_subtype')).toBeInTheDocument();
  });

  it('renders no subtype badge when the context field is absent', () => {
    render(
      <TaskDetailDrawer
        task={makeTask({ status: 'FAILED', failure_class: 'AGENT_TIMEOUT', context: {} })}
        logLines={[]}
        {...baseHandlers}
      />
    );
    expect(screen.queryByText('Subtype:')).not.toBeInTheDocument();
  });

  it('clicking the close button fires onClose', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <TaskDetailDrawer task={makeTask()} logLines={[]} {...baseHandlers} onClose={onClose} />
    );
    // Close button is the sibling of the fullscreen button (which has a title attribute)
    const fullscreenBtn = screen.getByTitle('Fullscreen (f)');
    await user.click(fullscreenBtn.nextElementSibling as HTMLElement);
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('shows Autonomous merge badge for ORCHESTRATOR-created merged task', () => {
    render(
      <TaskDetailDrawer
        task={makeTask({ created_by: 'ORCHESTRATOR', pr_url: 'https://github.com/example/repo/pull/1', status: 'COMPLETED' })}
        logLines={[]}
        {...baseHandlers}
      />
    );
    expect(screen.getByText('Autonomous merge')).toBeInTheDocument();
    expect(screen.queryByText('Operator-assisted merge')).not.toBeInTheDocument();
  });

  it('shows Operator-assisted merge badge when review_dismissed is true', () => {
    render(
      <TaskDetailDrawer
        task={makeTask({
          created_by: 'ORCHESTRATOR',
          pr_url: 'https://github.com/example/repo/pull/2',
          status: 'COMPLETED',
          context: { review_dismissed: true },
        })}
        logLines={[]}
        {...baseHandlers}
      />
    );
    expect(screen.getByText('Operator-assisted merge')).toBeInTheDocument();
    expect(screen.queryByText('Autonomous merge')).not.toBeInTheDocument();
  });

  it('shows no merge badge for non-merged tasks', () => {
    render(
      <TaskDetailDrawer
        task={makeTask({ status: 'IN_PROGRESS' })}
        logLines={[]}
        {...baseHandlers}
      />
    );
    expect(screen.queryByText('Autonomous merge')).not.toBeInTheDocument();
    expect(screen.queryByText('Operator-assisted merge')).not.toBeInTheDocument();
  });

  it('copy logs button calls clipboard.writeText with the log content', async () => {
    const user = userEvent.setup();
    // userEvent.setup() installs its own clipboard stub (configurable: true).
    // Override it now so the component writes to our spy instead.
    Object.defineProperty(window.navigator, 'clipboard', {
      value: { writeText: writeTextSpy },
      writable: true,
      configurable: true,
    });
    render(
      <TaskDetailDrawer
        task={makeTask()}
        logLines={['line one', 'line two']}
        {...baseHandlers}
      />
    );
    await user.click(screen.getByText('Logs'));
    await user.click(screen.getByTitle('Copy logs'));
    expect(writeTextSpy).toHaveBeenCalledWith('line one\nline two');
  });

  it('shows the Panel review button for a review task with PR context and invokes onPanelReview', async () => {
    const user = userEvent.setup();
    const onPanelReview = vi.fn();
    const task = makeTask({
      task_type: 'review',
      payloads: [{ pr_number: 88, branch: 'feature/x', base_branch: 'automain' }],
    });
    render(
      <TaskDetailDrawer task={task} logLines={[]} {...baseHandlers} onPanelReview={onPanelReview} />,
    );
    const btn = screen.getByTitle('Panel review this PR');
    await user.click(btn);
    expect(onPanelReview).toHaveBeenCalledTimes(1);
    expect(onPanelReview).toHaveBeenCalledWith(task);
  });

  it('does not show the Panel review button for a non-review/fix task', () => {
    const task = makeTask({ task_type: 'create', payloads: [{ pr_number: 5 }] });
    render(
      <TaskDetailDrawer task={task} logLines={[]} {...baseHandlers} onPanelReview={vi.fn()} />,
    );
    expect(screen.queryByTitle('Panel review this PR')).toBeNull();
  });

  it('does not show the Panel review button when a review task lacks PR context', () => {
    const task = makeTask({ task_type: 'review', payloads: [{}] });
    render(
      <TaskDetailDrawer task={task} logLines={[]} {...baseHandlers} onPanelReview={vi.fn()} />,
    );
    expect(screen.queryByTitle('Panel review this PR')).toBeNull();
  });

  it('resets transient form state when navigating between tasks', async () => {
    const user = userEvent.setup();
    const task1 = makeTask({
      id: 'task-1',
      title: 'Task One',
      requires_approval: true,
      status: 'EVALUATING',
    });
    const task2 = makeTask({
      id: 'task-2',
      title: 'Task Two',
      requires_approval: true,
      status: 'EVALUATING',
    });

    const { rerender } = render(
      <TaskDetailDrawer task={task1} logLines={[]} {...baseHandlers} />,
    );

    // Open Request Changes form on task 1
    await user.click(screen.getByText('Request Changes'));
    expect(screen.getByPlaceholderText('Describe what needs to change...')).toBeInTheDocument();

    // Type feedback into the form
    const feedbackInput = screen.getByPlaceholderText('Describe what needs to change...');
    await user.type(feedbackInput, 'Please fix this');

    // Verify feedback is in the form
    expect(feedbackInput).toHaveValue('Please fix this');

    // Navigate to task 2
    rerender(
      <TaskDetailDrawer task={task2} logLines={[]} {...baseHandlers} />,
    );

    // Verify the form is closed and feedback is cleared
    expect(screen.queryByPlaceholderText('Describe what needs to change...')).not.toBeInTheDocument();
  });

  it('resets cancel confirmation state when navigating between tasks', async () => {
    const user = userEvent.setup();
    const task1 = makeTask({
      id: 'task-1',
      title: 'Task One',
      status: 'IN_PROGRESS',
    });
    const task2 = makeTask({
      id: 'task-2',
      title: 'Task Two',
      status: 'IN_PROGRESS',
    });

    const { rerender } = render(
      <TaskDetailDrawer task={task1} logLines={[]} {...baseHandlers} />,
    );

    // Click Cancel Task to show confirmation
    await user.click(screen.getByText('Cancel Task'));
    expect(screen.getByText('Cancel this task?')).toBeInTheDocument();

    // Navigate to task 2
    rerender(
      <TaskDetailDrawer task={task2} logLines={[]} {...baseHandlers} />,
    );

    // Verify the confirmation is gone
    expect(screen.queryByText('Cancel this task?')).not.toBeInTheDocument();
    // The button should be back to the initial state
    expect(screen.getByText('Cancel Task')).toBeInTheDocument();
  });

  it('edit prompt editor seeds from the current payload prompt after a prior edit', async () => {
    const user = userEvent.setup();
    const editedPrompt = 'This is the edited prompt from the payload';
    const originalDescription = 'Original task description';
    const task = makeTask({
      status: 'FAILED',
      description: originalDescription,
      payloads: [{ prompt: editedPrompt }],
    });

    render(
      <TaskDetailDrawer task={task} logLines={[]} {...baseHandlers} />,
    );

    // Click the Edit button to open the prompt editor
    await user.click(screen.getByText('Edit'));

    // The input field should be populated with the payload prompt, not the description
    const input = screen.getByDisplayValue(editedPrompt);
    expect(input).toBeInTheDocument();
    expect(input).toHaveValue(editedPrompt);

    // Verify it's not showing the original description
    expect(input).not.toHaveValue(originalDescription);
  });
});
