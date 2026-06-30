import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import TaskStreamPanel from './TaskStreamPanel';
import { expectNoOverflowAt } from '../test/viewport';
import type { Task, TrackLogEntry } from '../types/task';

// TaskStreamPanel has no '../api' import (pure prop-driven realtime UI).
// It is a position:fixed bottom-right tracker — at width 400 with right=0,
// the panel's bounding rect right edge sits exactly at the viewport right
// edge regardless of viewport width, so it does not push horizontal scroll.
// Children are clipped to the panel's content-box (overflow-y-auto on the
// log area, custom-scrollbar on the tab strip) so they don't extend past
// the panel either. We exercise both the single-task and multi-tab paths.

const taskFixture: Task = {
  id: 'task-stream-1',
  project_id: 'proj-1',
  title: 'feat: a moderately long task title that the tracker truncates',
  description: '',
  status: 'IN_PROGRESS',
  task_type: 'create',
  created_by: 'tester',
  requires_approval: false,
  created_at: '2026-05-06T00:00:00Z',
};

const taskFixture2: Task = {
  ...taskFixture,
  id: 'task-stream-2',
  title: 'fix: another tracked task with an even longer title for tab truncation',
  status: 'COMPLETED',
};

const sampleLogs: TrackLogEntry[] = [
  { id: 1, t: '2026-05-06T00:00:01Z', type: 'system', msg: 'started tracking' },
  { id: 2, t: '2026-05-06T00:00:02Z', type: 'log', msg: 'agent.run: kicking off iteration 1' },
  { id: 3, t: '2026-05-06T00:00:03Z', type: 'status', msg: 'IN_PROGRESS' },
  { id: 4, t: '2026-05-06T00:00:04Z', type: 'error', msg: 'an error message that is intentionally long enough to require word-wrap inside the narrow log column on a 375px-wide phone' },
  { id: 5, t: '2026-05-06T00:00:05Z', type: 'artifact', msg: 'pr_url=https://github.com/example/repo/pull/12345' },
];

function assertFloatingPanelFits(width: number) {
  const panel = document.querySelector<HTMLDivElement>('[class*="fixed"][class*="bottom-0"]');
  if (!panel) {
    throw new Error('TaskStreamPanel floating panel was not rendered');
  }
  if (panel.dataset.variant !== 'floating') {
    throw new Error(`TaskStreamPanel default variant was ${panel.dataset.variant || 'unset'}, expected floating`);
  }
  const rect = panel.getBoundingClientRect();
  if (rect.left < 0) {
    throw new Error(`TaskStreamPanel left edge (${Math.round(rect.left)}px) extends off-screen at ${width}px viewport`);
  }
  if (rect.right > width) {
    throw new Error(`TaskStreamPanel right edge (${Math.round(rect.right)}px) exceeds ${width}px viewport`);
  }
}

describe('TaskStreamPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('single tracked task renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <TaskStreamPanel
            trackedTaskIds={new Set([taskFixture.id])}
            trackedLogs={new Map([[taskFixture.id, sampleLogs]])}
            tasks={[taskFixture]}
            onUntrack={() => {}}
            onUntrackAll={() => {}}
          />,
        );
      });
      // Assert the default floating panel's bounding box fits entirely within viewport (both edges).
      assertFloatingPanelFits(375);
    });
  });

  test('multiple tracked tasks (tab strip) does not overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <TaskStreamPanel
            trackedTaskIds={new Set([taskFixture.id, taskFixture2.id])}
            trackedLogs={new Map([
              [taskFixture.id, sampleLogs],
              [taskFixture2.id, sampleLogs.slice(0, 2)],
            ])}
            tasks={[taskFixture, taskFixture2]}
            onUntrack={() => {}}
            onUntrackAll={() => {}}
          />,
        );
      });
      // Assert the default floating panel's bounding box fits entirely within viewport (both edges).
      assertFloatingPanelFits(375);
    });
  });
});
