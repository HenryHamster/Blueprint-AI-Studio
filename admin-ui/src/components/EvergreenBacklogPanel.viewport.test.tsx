import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { EvergreenBacklogPanel } from './EvergreenBacklogPanel';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api');
  return {
    ...actual,
    fetchEvergreenBacklog: vi.fn(async () => ({
      items: [
        {
          id: 'item-1',
          project_id: 'project-123',
          direction_id: 'dir-1',
          direction_text: 'Improve admin UI mobile responsiveness across all operator-facing panels',
          status: 'enrolled',
          created_at: '2026-06-01T00:00:00Z',
          spawned_at: null,
          done_at: null,
          task_id: null,
        },
        {
          id: 'item-2',
          project_id: 'project-123',
          direction_id: 'dir-2',
          direction_text: 'Add comprehensive test coverage for evergreen backlog lifecycle transitions',
          status: 'spawned',
          created_at: '2026-06-01T01:00:00Z',
          spawned_at: '2026-06-01T02:00:00Z',
          done_at: null,
          task_id: 'task-abc',
        },
      ],
    })),
    fetchEvergreenBacklogSummary: vi.fn(async () => ({
      enrolled: 1,
      spawned: 1,
      done: 5,
      retired: 2,
    })),
  };
});

describe('EvergreenBacklogPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(<EvergreenBacklogPanel projectId="project-123" />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    }, { waitMs: 50 });
  });

  test('renders without horizontal overflow at 1280px', async () => {
    await expectNoOverflowAt(1280, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(<EvergreenBacklogPanel projectId="project-123" />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    }, { waitMs: 50 });
  });
});
