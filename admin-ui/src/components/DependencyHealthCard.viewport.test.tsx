import { afterEach, beforeEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { DependencyHealthCard } from './DependencyHealthCard';
import { expectNoOverflowAt } from '../test/viewport';
import type { DependencyHealthSignals } from '../types/digest';

function makeData(overrides: Partial<DependencyHealthSignals> = {}): DependencyHealthSignals {
  return {
    parked_blockers: {
      total: 2,
      tasks: [
        {
          task_id: 't1',
          title: 'Parked alpha',
          project_id: 'p1',
          blocker_kind: 'Base re-cut conflict',
          blocked_reason: 'held',
          blocked_at: '2026-06-01T00:00:00',
          recheck_after: null,
        },
      ],
    },
    duplicate_foundation_candidates: {
      total: 1,
      candidates: [{ task_ids: ['a', 'b'], overlap_ratio: 0.5, shared_files: ['foo.py'] }],
    },
    unresumable_cancellations: {
      total: 0,
      by_class: {},
      by_direction: {},
      tasks: [],
    },
    ...overrides,
  };
}

describe('DependencyHealthCard viewport', () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    container = document.createElement('div');
    container.style.padding = '12px';
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    document.body.removeChild(container);
  });

  test('renders without overflow at 375px (mobile)', async () => {
    await expectNoOverflowAt(375, async () => {
      act(() => {
        root.render(<DependencyHealthCard data={makeData()} />);
      });
    });
  });

  test('renders without overflow at 768px (tablet)', async () => {
    await expectNoOverflowAt(768, async () => {
      act(() => {
        root.render(<DependencyHealthCard data={makeData()} />);
      });
    });
  });

  test('renders without overflow at 1024px (desktop)', async () => {
    await expectNoOverflowAt(1024, async () => {
      act(() => {
        root.render(<DependencyHealthCard data={makeData()} />);
      });
    });
  });
});
