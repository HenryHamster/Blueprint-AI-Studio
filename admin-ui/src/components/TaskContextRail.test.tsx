import { describe, expect, test, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { TaskContextRail } from './TaskContextRail';
import type { Task } from '../types/task';
import type { StrategicWorkbenchState } from '../strategicStateApi';

type SessionRefs = StrategicWorkbenchState['selected_refs'];

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-12345678',
    project_id: 'proj-1',
    title: 'Implement active work context',
    description: 'Task description',
    status: 'IN_PROGRESS',
    task_type: 'create',
    created_by: 'ORCHESTRATOR',
    requires_approval: false,
    created_at: '2026-06-29T12:00:00Z',
    started_at: '2026-06-29T12:05:00Z',
    latest_status_message: 'Reviewing the operator sideboard.',
    payloads: [{ type: 'create' }],
    context: {},
    sideboard_context: {
      project: { id: 'proj-1', name: 'Blueprint', repo: 'HenryHamster/BlueprintAIStudio' },
      purpose: 'Active work context',
      strategic: {
        direction: {
          id: 'd-1',
          text: 'Keep active work tied to Strategic context.',
          priority: 'high',
          status: 'active',
        },
        cycle: null,
      },
      pipeline: {
        parent_task_id: null,
        child_count: 2,
        dependency_count: 0,
        blocked_reason: null,
      },
      artifacts: {
        total: 1,
        by_type: { PULL_REQUEST: 1 },
        primary_pr_url: 'https://github.com/HenryHamster/BlueprintAIStudio/pull/42',
        primary_commit: null,
      },
      messages: { total_recent: 0, items: [] },
      links: [],
    },
    ...overrides,
  };
}

describe('TaskContextRail', () => {
  test('renders the current task context and opens details', async () => {
    const user = userEvent.setup();
    const onOpenTask = vi.fn();
    render(
      <TaskContextRail
        currentTask={makeTask()}
        attentionTasks={[]}
        trackedTaskIds={new Set()}
        onOpenTask={onOpenTask}
      />,
    );

    expect(screen.getByText('Task context')).toBeInTheDocument();
    expect(screen.getByText('Implement active work context')).toBeInTheDocument();
    expect(screen.getByText('Reviewing the operator sideboard.')).toBeInTheDocument();
    expect(screen.getByText('Keep active work tied to Strategic context.')).toBeInTheDocument();
    expect(screen.getByText('2 child tasks')).toBeInTheDocument();

    await user.click(screen.getByText('Details'));
    expect(onOpenTask).toHaveBeenCalledWith(expect.objectContaining({ id: 'task-12345678' }));
  });

  test('uses sideboard pipeline dependency counts when dependency ids are absent', () => {
    const sideboardContext = makeTask().sideboard_context as NonNullable<Task['sideboard_context']>;
    render(
      <TaskContextRail
        currentTask={makeTask({
          depends_on: undefined,
          sideboard_context: {
            ...sideboardContext,
            pipeline: {
              ...sideboardContext.pipeline,
              dependency_count: 3,
            },
          },
        })}
        attentionTasks={[]}
        trackedTaskIds={new Set()}
        onOpenTask={vi.fn()}
      />,
    );

    expect(screen.getByText('waiting on 3')).toBeInTheDocument();
  });

  test('renders compact task facts when optional Task fields are present', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-29T12:35:00Z'));
    const sideboardContext = makeTask().sideboard_context as NonNullable<Task['sideboard_context']>;

    try {
      render(
        <TaskContextRail
          currentTask={makeTask({
            last_heartbeat: '2026-06-29T12:30:00Z',
            last_tool_use_at: '2026-06-29T12:25:00Z',
            estimated_cost_usd: 4.25,
            vm_machine_type: 'n2-standard-4',
            vm_warm_start: true,
            vm_spot: true,
            vm_instance_id: 'vm-blueprint-1',
            task_llm_cost_usd: 0.1234,
            task_llm_cost_any_unpriced: true,
            api_call_summary: {
              llm_call_count: 11,
              total_calls: 19,
              provider: 'openai',
              model: 'gpt-5-mini',
            } as NonNullable<Task['api_call_summary']> & {
              total_calls: number;
              provider: string;
              model: string;
            },
            context: { runner_target_id: 'runner-west-1' },
            sideboard_context: {
              ...sideboardContext,
              purpose: 'Passive task context',
              artifacts: { ...sideboardContext.artifacts, total: 3 },
              messages: { ...sideboardContext.messages, total_recent: 4 },
            },
          })}
          attentionTasks={[]}
          onOpenTask={vi.fn()}
        />,
      );

      expect(screen.getByText('$4.25')).toBeInTheDocument();
      expect(screen.getByTestId('task-rail-fact-heartbeat')).toHaveTextContent('5m ago');
      expect(screen.getByTestId('task-rail-fact-tool')).toHaveTextContent('10m ago');
      expect(screen.getByTestId('task-rail-fact-runner')).toHaveTextContent('n2-standard-4');
      expect(screen.getByTestId('task-rail-fact-runner')).toHaveTextContent('warm start');
      expect(screen.getByTestId('task-rail-fact-runner')).toHaveTextContent('spot');
      expect(screen.getByTestId('task-rail-fact-runner')).toHaveTextContent('vm vm-blueprint-1');
      expect(screen.getByTestId('task-rail-fact-runner')).toHaveTextContent('runner runner-west-1');
      expect(screen.getByTestId('task-rail-fact-llm')).toHaveTextContent('~$0.1234');
      expect(screen.getByTestId('task-rail-fact-llm')).toHaveTextContent('19 calls');
      expect(screen.getByTestId('task-rail-fact-llm')).toHaveTextContent('openai/gpt-5-mini');
      expect(screen.getByTestId('task-rail-fact-sideboard')).toHaveTextContent('Passive task context');
      expect(screen.getByTestId('task-rail-fact-sideboard')).toHaveTextContent('3 artifacts');
      expect(screen.getByTestId('task-rail-fact-sideboard')).toHaveTextContent('4 messages');
    } finally {
      vi.useRealTimers();
    }
  });

  test('omits compact task facts when optional Task fields are absent', () => {
    render(
      <TaskContextRail
        currentTask={makeTask({
          sideboard_context: null,
          depends_on: [],
          child_task_ids: [],
          last_heartbeat: undefined,
          last_tool_use_at: null,
          vm_instance_id: undefined,
          vm_machine_type: null,
          vm_warm_start: false,
          vm_spot: false,
          task_llm_cost_usd: 0,
          api_call_summary: { llm_call_count: 0, total_calls: 0 } as NonNullable<Task['api_call_summary']> & {
            total_calls: number;
          },
          context: {},
        })}
        attentionTasks={[]}
        onOpenTask={vi.fn()}
      />,
    );

    expect(screen.queryByTestId('task-rail-fact-heartbeat')).not.toBeInTheDocument();
    expect(screen.queryByTestId('task-rail-fact-tool')).not.toBeInTheDocument();
    expect(screen.queryByTestId('task-rail-fact-runner')).not.toBeInTheDocument();
    expect(screen.queryByTestId('task-rail-fact-llm')).not.toBeInTheDocument();
    expect(screen.queryByTestId('task-rail-fact-sideboard')).not.toBeInTheDocument();
    expect(screen.queryByText(/recent message/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/artifact/i)).not.toBeInTheDocument();
  });

  test('renders attention tasks when no task is currently open', async () => {
    const user = userEvent.setup();
    const onOpenTask = vi.fn();
    const failed = makeTask({
      id: 'task-failed',
      title: 'Fix failed run',
      status: 'FAILED',
      latest_status_message: 'Tests failed.',
    });
    render(
      <TaskContextRail
        currentTask={null}
        attentionTasks={[failed]}
        onOpenTask={onOpenTask}
      />,
    );

    expect(screen.getByText('Fix failed run')).toBeInTheDocument();
    await user.click(screen.getByText('Fix failed run'));
    expect(onOpenTask).toHaveBeenCalledWith(expect.objectContaining({ id: 'task-failed' }));
  });

  test('keeps other attention tasks visible without duplicating the current task', () => {
    const task = makeTask();
    render(
      <TaskContextRail
        currentTask={task}
        attentionTasks={[task, makeTask({ id: 'task-blocked', title: 'Blocked task', status: 'BLOCKED' })]}
        onOpenTask={vi.fn()}
      />,
    );

    expect(screen.getAllByText('Implement active work context')).toHaveLength(1);
    expect(screen.getByText('Needs attention')).toBeInTheDocument();
    expect(screen.getByText('Blocked task')).toBeInTheDocument();
  });

  test('renders a quiet empty state', () => {
    render(<TaskContextRail currentTask={null} attentionTasks={[]} onOpenTask={vi.fn()} />);

    expect(screen.getByText('No task selected.')).toBeInTheDocument();
  });

  test('pipeline and track actions use existing callbacks', async () => {
    const user = userEvent.setup();
    const onOpenPipeline = vi.fn();
    const onTrackTask = vi.fn();
    const task = makeTask();
    render(
      <TaskContextRail
        currentTask={task}
        attentionTasks={[]}
        trackedTaskIds={new Set()}
        onOpenTask={vi.fn()}
        onOpenPipeline={onOpenPipeline}
        onTrackTask={onTrackTask}
      />,
    );

    await user.click(screen.getByText('Pipeline'));
    await user.click(screen.getByText('Track'));
    expect(onOpenPipeline).toHaveBeenCalled();
    expect(onTrackTask).toHaveBeenCalledWith(task);
  });

  test('keeps the current task in Strategic session context', async () => {
    const user = userEvent.setup();
    const task = makeTask();
    const onUpdateSessionRefs = vi.fn();
    render(
      <TaskContextRail
        currentTask={task}
        attentionTasks={[]}
        sessionRefs={[]}
        onOpenTask={vi.fn()}
        onUpdateSessionRefs={onUpdateSessionRefs}
      />,
    );

    await user.click(screen.getByRole('button', { name: /keep context/i }));
    expect(onUpdateSessionRefs).toHaveBeenCalledWith(expect.any(Function));
    const updater = onUpdateSessionRefs.mock.calls[0][0] as (refs: SessionRefs) => SessionRefs;
    expect(updater([{ kind: 'direction', id: 'd-1', label: 'Direction' }])).toEqual([
      { kind: 'task', id: task.id, label: task.title },
      { kind: 'direction', id: 'd-1', label: 'Direction' },
    ]);
    expect(updater([{ kind: 'task', id: task.id, label: 'Old task label' }])).toEqual([
      { kind: 'task', id: task.id, label: task.title },
    ]);
  });

  test('removes the current task from Strategic session context', async () => {
    const user = userEvent.setup();
    const task = makeTask();
    const onUpdateSessionRefs = vi.fn();
    render(
      <TaskContextRail
        currentTask={task}
        attentionTasks={[]}
        sessionRefs={[{ kind: 'task', id: task.id, label: task.title }]}
        onOpenTask={vi.fn()}
        onUpdateSessionRefs={onUpdateSessionRefs}
      />,
    );

    await user.click(screen.getByRole('button', { name: /remove context/i }));
    expect(onUpdateSessionRefs).toHaveBeenCalledWith(expect.any(Function));
    const updater = onUpdateSessionRefs.mock.calls[0][0] as (refs: SessionRefs) => SessionRefs;
    expect(updater([
      { kind: 'direction', id: 'd-1', label: 'Direction' },
      { kind: 'task', id: task.id, label: task.title },
    ])).toEqual([{ kind: 'direction', id: 'd-1', label: 'Direction' }]);
  });

  test('disables session context action while refs are saving', async () => {
    const user = userEvent.setup();
    const onUpdateSessionRefs = vi.fn();
    render(
      <TaskContextRail
        currentTask={makeTask()}
        attentionTasks={[]}
        sessionRefs={[]}
        savingSessionRefs
        onOpenTask={vi.fn()}
        onUpdateSessionRefs={onUpdateSessionRefs}
      />,
    );

    const button = screen.getByRole('button', { name: /keep context/i });
    expect(button).toBeDisabled();
    await user.click(button);
    expect(onUpdateSessionRefs).not.toHaveBeenCalled();
  });

  test('omits session context action when session props are not wired', () => {
    render(
      <TaskContextRail
        currentTask={makeTask()}
        attentionTasks={[]}
        onOpenTask={vi.fn()}
      />,
    );

    expect(screen.queryByRole('button', { name: /keep context/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /remove context/i })).not.toBeInTheDocument();
  });

  test('omits session context action while session refs are not loaded', () => {
    render(
      <TaskContextRail
        currentTask={makeTask()}
        attentionTasks={[]}
        onOpenTask={vi.fn()}
        onUpdateSessionRefs={vi.fn()}
      />,
    );

    expect(screen.queryByRole('button', { name: /keep context/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /remove context/i })).not.toBeInTheDocument();
  });

  test('merges backend attention tasks with local fallback tasks', async () => {
    const user = userEvent.setup();
    const onOpenTask = vi.fn();
    const backendTask = makeTask({ id: 'task-backend', title: 'Backend attention task', status: 'BLOCKED' });
    const localTask = makeTask({ id: 'task-local', title: 'Local fallback task', status: 'FAILED' });
    const overlapTask = makeTask({ id: 'task-overlap', title: 'Overlapping task', status: 'BLOCKED' });

    render(
      <TaskContextRail
        currentTask={null}
        attentionTasks={[backendTask, overlapTask]}
        localFallbackTasks={[overlapTask, localTask]}
        onOpenTask={onOpenTask}
      />,
    );

    expect(screen.getByText('Backend attention task')).toBeInTheDocument();
    expect(screen.getByText('Local fallback task')).toBeInTheDocument();
    expect(screen.getAllByText('Overlapping task')).toHaveLength(1);

    await user.click(screen.getByText('Local fallback task'));
    expect(onOpenTask).toHaveBeenCalledWith(expect.objectContaining({ id: 'task-local' }));
  });

  test('prefers exact sideboard child count over capped child task ids', () => {
    const sideboardContext = makeTask().sideboard_context as NonNullable<Task['sideboard_context']>;
    render(
      <TaskContextRail
        currentTask={makeTask({
          child_task_ids: Array.from({ length: 1000 }, (_, idx) => `child-${idx}`),
          sideboard_context: {
            ...sideboardContext,
            pipeline: {
              parent_task_id: null,
              child_count: 1002,
              dependency_count: 0,
              blocked_reason: null,
            },
          },
        })}
        attentionTasks={[]}
        onOpenTask={vi.fn()}
      />,
    );

    expect(screen.getByText('1,002 child tasks')).toBeInTheDocument();
    expect(screen.queryByText('1,000 child tasks')).not.toBeInTheDocument();
  });
});
