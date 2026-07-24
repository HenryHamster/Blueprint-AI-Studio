import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, test, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Task } from '../types/task';
import type { ProjectMessagePreview } from '../types/messages';
import { TaskSideboardContextSection } from './TaskDetailDrawerSideboardContext';

const BACKEND_SIDEBOARD_FIXTURE = JSON.parse(
  readFileSync(
    path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      '../../../backend/testing/__fixtures__/operator_task_sideboard_context_v1.json',
    ),
    'utf-8',
  ),
) as { task: Task };

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-12345678',
    project_id: 'proj-1',
    title: 'Implement sideboard polish',
    description: 'Task description',
    status: 'IN_PROGRESS',
    task_type: 'create',
    created_by: 'ORCHESTRATOR',
    requires_approval: false,
    created_at: '2026-06-29T12:00:00Z',
    sideboard_context: {
      task: {
        id: 'task-12345678',
        title: 'Implement sideboard polish',
        status: 'IN_PROGRESS',
        task_type: 'create',
        latest_status_message: 'Implementing compact sideboard rows.',
        blocker: {
          kind: 'dependency',
          reason: 'Waiting for upstream verification.',
          blocked_at: '2026-06-29T13:00:00Z',
          recheck_after: '2026-06-29T14:00:00Z',
          parked_at: null,
        },
        runner: {
          vm_instance_id: 'agent-vm-sideboard',
          runner_target_id: 'runner-local-1',
          machine_type: 'n2-standard-4',
          spot: true,
          warm_start: false,
          provider: 'anthropic',
          account_id: 'acct-sideboard',
        },
        cost: {
          estimated_vm_usd: 1.25,
          llm_usd: 0.42,
          llm_any_unpriced: false,
        },
        next_action: {
          label: 'Monitor progress',
          reason: 'Implementing compact sideboard rows.',
          target: { surface: 'task', id: 'task-12345678', task_id: 'task-12345678' },
        },
      },
      project: { id: 'proj-1', name: 'Blueprint', repo: 'HenryHamster/BlueprintAIStudio' },
      purpose: 'Make passive work readable from the task drawer.',
      strategic: {
        direction: {
          id: 'direction-long-running-work',
          text: 'Keep monitoring context dense and legible for passive tasks.',
          priority: 'high',
          status: 'active',
        },
        cycle: {
          id: 'cycle-2026-06-30',
          phase: 'implementation_review',
          outcome: 'accepted',
        },
      },
      pipeline: {
        parent_task_id: 'parent-task-abcdef',
        child_count: 3,
        dependency_count: 2,
        blocked_reason: 'Waiting for upstream verification.',
      },
      artifacts: {
        total: 2,
        by_type: { PULL_REQUEST: 1, LOG: 1 },
        primary_pr_url: 'https://github.com/HenryHamster/BlueprintAIStudio/pull/42',
        primary_commit: 'abcdef1234567890',
      },
      messages: {
        total_recent: 3,
        items: [
          { id: 'message-1', role: 'assistant', content: 'Validated the compact sideboard rows.' },
          { id: 'message-2', role: 'user', content: 'Keep all existing task context visible.' },
        ] as unknown as ProjectMessagePreview[],
      },
      links: [
        {
          id: 'link-task',
          label: 'Open child task',
          target: { surface: 'task', id: 'child-task-123456', task_id: 'child-task-123456' },
        },
        {
          id: 'link-external',
          label: 'Review evidence',
          target: { surface: 'external', id: 'https://example.com/evidence', url: 'https://example.com/evidence' },
        },
      ],
    },
    ...overrides,
  };
}

describe('TaskSideboardContextSection', () => {
  test('groups sideboard context into an integrated workbench surface without dropping passive work details', async () => {
    const user = userEvent.setup();
    const onNavigateToTask = vi.fn();
    render(<TaskSideboardContextSection task={makeTask()} onNavigateToTask={onNavigateToTask} />);

    expect(screen.getByText('Context')).toBeInTheDocument();
    expect(screen.getByText('Task')).toBeInTheDocument();
    expect(screen.getByText('Current blocker')).toBeInTheDocument();
    expect(screen.getByText('Runner')).toBeInTheDocument();
    expect(screen.getByText('Project')).toBeInTheDocument();
    expect(screen.getByText('Strategic')).toBeInTheDocument();
    expect(screen.getByText('Pipeline')).toBeInTheDocument();
    expect(screen.getByText('Evidence')).toBeInTheDocument();
    expect(screen.getByText('Links')).toBeInTheDocument();
    expect(screen.getByText('Messages')).toBeInTheDocument();
    expect(screen.getByText('HenryHamster/BlueprintAIStudio')).toBeInTheDocument();
    expect(screen.getByText('Monitor progress')).toBeInTheDocument();
    expect(screen.getAllByText('Implementing compact sideboard rows.').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('agent-vm-sideboard / runner runner-local-1 / n2-standard-4 / anthropic / acct-sideboard')).toBeInTheDocument();
    expect(screen.getByText('$1.25')).toBeInTheDocument();
    expect(screen.getByText('$0.42')).toBeInTheDocument();
    expect(screen.getByText('Make passive work readable from the task drawer.')).toBeInTheDocument();
    expect(screen.getByText('Keep monitoring context dense and legible for passive tasks.')).toBeInTheDocument();
    expect(screen.getByText('cycle-2026-06-30')).toBeInTheDocument();
    expect(screen.getByText('implementation review')).toBeInTheDocument();
    expect(screen.getByText('Dependencies')).toBeInTheDocument();
    expect(screen.getByText('Children')).toBeInTheDocument();
    expect(screen.getAllByText('Waiting for upstream verification.').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('pull request 1 · log 1')).toBeInTheDocument();
    expect(screen.getByText('abcdef123456')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'PR' })).toHaveAttribute(
      'href',
      'https://github.com/HenryHamster/BlueprintAIStudio/pull/42',
    );
    expect(screen.getByText('Open child task')).toBeInTheDocument();
    expect(screen.getByText('Review evidence')).toBeInTheDocument();
    expect(screen.getByText('Validated the compact sideboard rows.')).toBeInTheDocument();
    expect(screen.getByText('+1 more recent message')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /Open child task/i }));
    expect(onNavigateToTask).toHaveBeenCalledWith('child-task-123456');

    await user.click(screen.getByRole('button', { name: 'parent parent-task-abcdef' }));
    expect(onNavigateToTask).toHaveBeenCalledWith('parent-task-abcdef');
  });

  test('renders the backend v1 sideboard fixture shape', () => {
    render(<TaskSideboardContextSection task={BACKEND_SIDEBOARD_FIXTURE.task} onNavigateToTask={vi.fn()} />);

    expect(screen.getByText('Task')).toBeInTheDocument();
    expect(screen.getByText('Current blocker')).toBeInTheDocument();
    expect(screen.getByText('Runner')).toBeInTheDocument();
    expect(screen.getAllByText('blocked').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('Resolve blocker')).toBeInTheDocument();
    expect(screen.getAllByText('Waiting for upstream verification.').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('agent-vm-sideboard / runner runner-local-1 / n2-standard-4 / anthropic / acct-sideboard')).toBeInTheDocument();
    expect(screen.getByText('$1.25')).toBeInTheDocument();
    expect(screen.getByText('$0.42')).toBeInTheDocument();
    expect(screen.getByText('Keep active work understandable.')).toBeInTheDocument();
    expect(screen.getByText('Keep passive work legible from the operator sideboard.')).toBeInTheDocument();
    expect(screen.getByText('task-parent-wave')).toBeInTheDocument();
    expect(screen.getByText('pull request 1 · log 1')).toBeInTheDocument();
    expect(screen.getByText('The task is waiting on verification.')).toBeInTheDocument();
  });

  test('prefers pipeline movement_explanation over blocked_reason', () => {
    const task = makeTask();
    const context = task.sideboard_context!;
    render(
      <TaskSideboardContextSection
        task={{
          ...task,
          sideboard_context: {
            ...context,
            pipeline: {
              ...context.pipeline,
              blocked_reason: 'Raw pipeline blocker should stay hidden.',
              movement_explanation: 'Backend normalized pipeline movement.',
            },
          },
        }}
      />,
    );

    expect(screen.getByText('Backend normalized pipeline movement.')).toBeInTheDocument();
    expect(screen.queryByText('Raw pipeline blocker should stay hidden.')).not.toBeInTheDocument();
  });

  test('includes the pipeline group when movement_explanation is the only pipeline detail', () => {
    const task = makeTask();
    const context = task.sideboard_context!;
    render(
      <TaskSideboardContextSection
        task={{
          ...task,
          sideboard_context: {
            ...context,
            pipeline: {
              parent_task_id: null,
              child_count: 0,
              dependency_count: 0,
              blocked_reason: null,
              movement_explanation: 'Only movement detail is present.',
            },
          },
        }}
      />,
    );

    expect(screen.getByText('Pipeline')).toBeInTheDocument();
    expect(screen.getByText('Only movement detail is present.')).toBeInTheDocument();
  });

  test('shows a pin badge when preferred_runner_target_id is set', () => {
    const task = makeTask();
    const context = task.sideboard_context!;
    render(
      <TaskSideboardContextSection
        task={{
          ...task,
          sideboard_context: {
            ...context,
            task: {
              ...context.task!,
              runner: {
                ...context.task!.runner,
                preferred_runner_target_id: 'runner-9f8e7d6c-5b4a-3210-fedc-ba9876543210',
              },
            },
          },
        }}
      />,
    );

    const badge = screen.getByTestId('runner-pin-badge');
    expect(badge).toBeInTheDocument();
    // Long runner ids are truncated for display but preserved in the tooltip.
    expect(badge.textContent).toContain('pinned · runner-9f8e7d6c-5b...543210');
    expect(badge).toHaveAttribute('title', 'Pinned to runner runner-9f8e7d6c-5b4a-3210-fedc-ba9876543210');
    expect(screen.queryByTestId('runner-pin-hold')).not.toBeInTheDocument();
  });

  test('shows an amber hold callout with reason, detail, and runner id when runner_pin_hold is present', () => {
    const task = makeTask();
    const context = task.sideboard_context!;
    render(
      <TaskSideboardContextSection
        task={{
          ...task,
          sideboard_context: {
            ...context,
            task: {
              ...context.task!,
              runner: {
                ...context.task!.runner,
                preferred_runner_target_id: 'runner-local-1',
                runner_pin_hold: {
                  runner_id: 'runner-local-1',
                  strict: true,
                  reason_code: 'at_capacity',
                  detail: 'Runner iconlab has 2/2 slots in use; task waits for a free slot.',
                },
              },
            },
          },
        }}
      />,
    );

    const hold = screen.getByTestId('runner-pin-hold');
    expect(hold.textContent).toContain('Runner pin hold — at capacity');
    expect(hold.textContent).toContain('Runner iconlab has 2/2 slots in use; task waits for a free slot.');
    expect(hold.textContent).toContain('runner runner-local-1');
    expect(screen.getByTestId('runner-pin-badge')).toBeInTheDocument();
  });

  test('renders the Runner group when a pin hold is the only runner signal', () => {
    const task = makeTask();
    const context = task.sideboard_context!;
    render(
      <TaskSideboardContextSection
        task={{
          ...task,
          sideboard_context: {
            ...context,
            task: {
              ...context.task!,
              cost: undefined,
              runner: {
                preferred_runner_target_id: 'runner-offline-1',
                runner_pin_hold: {
                  runner_id: 'runner-offline-1',
                  strict: true,
                  reason_code: 'offline',
                  detail: 'No heartbeat for 12 minutes.',
                },
              },
            },
          },
        }}
      />,
    );

    expect(screen.getByText('Runner')).toBeInTheDocument();
    expect(screen.getByTestId('runner-pin-badge')).toBeInTheDocument();
    expect(screen.getByTestId('runner-pin-hold').textContent).toContain('offline');
  });
});
