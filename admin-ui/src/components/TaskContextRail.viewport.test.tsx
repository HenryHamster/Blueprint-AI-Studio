import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { TaskContextRail } from './TaskContextRail';
import type { Task } from '../types/task';
import { expectMinTouchTargetAt, expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';

const LONG_TASK: Task = {
  id: 'task-viewport-long-title',
  project_id: 'proj-1',
  title: 'Investigate strategic workbench task-sideboard integration for a long-running local workday session',
  description: 'Long description',
  status: 'IN_PROGRESS',
  task_type: 'create',
  created_by: 'ORCHESTRATOR',
  requires_approval: false,
  created_at: '2026-06-29T12:00:00Z',
  started_at: '2026-06-29T12:05:00Z',
  last_heartbeat: '2026-06-29T12:30:00Z',
  last_tool_use_at: '2026-06-29T12:25:00Z',
  latest_status_message: 'A very long status update explaining that the task is validating context handoff from Strategic chat into the pipeline task drawer without creating a second task management surface.',
  pr_url: 'https://github.com/HenryHamster/BlueprintAIStudio/pull/42',
  payloads: [{ type: 'create' }],
  context: {
    runner_target_id: 'runner-viewport-1',
    llm_provider: 'openai',
    llm_model: 'gpt-5-mini',
  },
  depends_on: ['task-upstream-1', 'task-upstream-2'],
  child_task_ids: ['task-child-1', 'task-child-2', 'task-child-3'],
  estimated_cost_usd: 4.25,
  vm_instance_id: 'vm-viewport-1',
  vm_machine_type: 'n2-standard-4',
  vm_warm_start: true,
  vm_spot: false,
  task_llm_cost_usd: 0.1876,
  api_call_summary: { llm_call_count: 12 },
  sideboard_context: {
    project: { id: 'proj-1', name: 'Blueprint', repo: 'HenryHamster/BlueprintAIStudio' },
    purpose: 'Task context viewport coverage',
    strategic: {
      direction: {
        id: 'd-long',
        text: 'Keep the task sideboard visually compact while still making active work understandable from the operator workbench rail.',
        priority: 'high',
        status: 'active',
      },
      cycle: null,
    },
    pipeline: { parent_task_id: null, child_count: 3, dependency_count: 2, blocked_reason: null },
    artifacts: { total: 2, by_type: { PULL_REQUEST: 1, LOG: 1 }, primary_pr_url: null, primary_commit: null },
    messages: { total_recent: 5, items: [] },
    links: [],
  },
};

describe('TaskContextRail viewport', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) act(() => { r.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  async function mount(width: number) {
    host = document.createElement('div');
    host.style.width = `${width}px`;
    document.body.appendChild(host);
    root = createRoot(host);
    const mountedRoot = root;
    await act(async () => {
      mountedRoot.render(
        <TaskContextRail
          currentTask={LONG_TASK}
          attentionTasks={[LONG_TASK]}
          trackedTaskIds={new Set()}
          sessionRefs={[]}
          onOpenTask={vi.fn()}
          onOpenPipeline={vi.fn()}
          onTrackTask={vi.fn()}
          onUpdateSessionRefs={vi.fn()}
        />,
      );
    });
  }

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => { await mount(375); });
  });

  test('buttons meet touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => { await mount(375); });
  });

  test('important labels are not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => { await mount(375); });
  });
});
