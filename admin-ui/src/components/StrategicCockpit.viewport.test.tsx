import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { StrategicCockpit } from './StrategicCockpit';
import { expectMinTouchTargetAt, expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';
import type { Task } from '../types/task';
import type { Alert } from '../types/alert';
import type { Direction } from './DirectionsPanel';

const longTitle = 'feat(operator): redesign Strategic cockpit for continuous workday steering without forcing polished direction essays';

const tasks: Task[] = [
  {
    id: 'task-review',
    title: longTitle,
    description: 'Review the operator cockpit direction and evidence bundle.',
    status: 'EVALUATING',
    task_type: 'review',
    created_by: 'agent',
    requires_approval: true,
    created_at: '2026-06-27T10:00:00Z',
    latest_status_message: 'Reviewing whether the UI supports daily check-ins and continuous steering.',
    context: { direction_id: 'dir-operator' },
  },
  {
    id: 'task-active',
    title: 'polish: turn task mechanics into workstream evidence and judgment cards',
    description: 'Implement visual hierarchy for cockpit workstreams.',
    status: 'IN_PROGRESS',
    task_type: 'create',
    created_by: 'agent',
    requires_approval: false,
    created_at: '2026-06-27T10:10:00Z',
    latest_status_message: 'Building a compact Strategic panel and mobile-safe mission cards.',
    context: { direction_id: 'dir-operator' },
  },
  {
    id: 'task-blocked',
    title: 'blocked: decide whether workflows should become first-class objects or stay as directions',
    description: 'Needs product judgment.',
    status: 'BLOCKED',
    task_type: 'research',
    created_by: 'agent',
    requires_approval: false,
    created_at: '2026-06-27T10:20:00Z',
    blocked_reason: 'Operator workflow model not settled.',
  },
  {
    id: 'task-complete',
    title: 'completed: preserve existing kanban and pipeline inspect layers',
    description: 'Proof artifact exists.',
    status: 'COMPLETED',
    task_type: 'fix',
    created_by: 'agent',
    requires_approval: false,
    created_at: '2026-06-27T09:20:00Z',
    pr_url: 'https://github.com/HenryHamster/BlueprintAIStudio/pull/12136',
  },
];

const directions: Direction[] = [
  {
    id: 'dir-operator',
    text: 'Make the operator experience feel like talking with Strategic while keeping evidence, workstreams, and decisions structured.',
    priority: 'high',
    added_at: '2026-06-27T09:00:00Z',
    status: 'active',
  },
];

const alerts: Alert[] = [
  {
    id: 'alert-operator',
    project_id: 'proj-1',
    severity: 'WARNING',
    title: 'Scope model still unsettled',
    message: 'Strategic needs a decision on whether workflows are separate objects or direction templates.',
    task_id: 'task-blocked',
    created_at: '2026-06-27T10:30:00Z',
    dismissed_at: null,
  },
];

describe('StrategicCockpit viewport', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) { host.remove(); host = null; }
  });

  function render() {
    host = document.createElement('div');
    document.body.appendChild(host);
    const nextRoot = createRoot(host);
    root = nextRoot;
    act(() => {
      nextRoot.render(
        <StrategicCockpit
          tasks={tasks}
          directions={directions}
          alerts={alerts}
          selectedProjectId="proj-1"
          projectName="BlueprintAIStudio"
          strategistActive
          onOpenComposer={vi.fn()}
          onOpenDirections={vi.fn()}
          onOpenResearch={vi.fn()}
          onCreateTask={vi.fn()}
          onViewTask={vi.fn()}
        />,
      );
    });
  }

  test('does not horizontally overflow at 375px', async () => {
    await expectNoOverflowAt(375, render);
  });

  test('does not horizontally overflow at 320px', async () => {
    await expectNoOverflowAt(320, render);
  });

  test('does not horizontally overflow at 1280px', async () => {
    await expectNoOverflowAt(1280, render);
  });

  test('does not silently clip cockpit text at 375px', async () => {
    await expectNoTextTruncationAt(375, render);
  });

  test('main actions meet mobile touch target size', async () => {
    await expectMinTouchTargetAt(375, render);
  });
});
