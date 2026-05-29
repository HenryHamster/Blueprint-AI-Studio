import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { CatchUpInProgressSection } from './CatchUpInProgressSection';
import { expectNoOverflowAt } from '../test/viewport';

describe('CatchUpInProgressSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('renders dense active-task rows without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <CatchUpInProgressSection
            activeCount={4}
            activeTasks={[
              {
                task_id: 'a',
                title: 'refactor: extract a very long descriptive section name from a large admin-ui panel component',
                status: 'running',
                task_type: 'refactor',
                started_at: '2026-05-29T08:00:00Z',
                direction_text: 'Decompose the largest admin-ui panels into smaller single-responsibility components',
                scope: 'large',
                review_iteration: 3,
                review_cap: 3,
                running_cost_usd: 12.34,
              },
              {
                task_id: 'b',
                title: 'fix: review feedback loop iteration with another lengthy title for overflow pressure',
                status: 'in_review',
                task_type: 'fix',
                started_at: '2026-05-29T07:30:00Z',
                direction_text: 'Cross-cutting codebase analysis spanning many backend modules and frontend panels',
                scope: 'medium',
                review_iteration: 2,
                review_cap: 3,
                running_cost_usd: 1.07,
              },
            ]}
            expanded
            onToggle={() => {}}
            onSelectTask={() => {}}
          />,
        );
      });
    });
  });
});
