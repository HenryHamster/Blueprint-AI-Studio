import { afterEach, beforeEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { MergeGateFleetHealthCard } from './MergeGateFleetHealthCard';
import { expectNoOverflowAt } from '../test/viewport';
import type { MergeGateFleetHealth } from '../types/digest';

function makeData(overrides: Partial<MergeGateFleetHealth> = {}): MergeGateFleetHealth {
  return {
    verified_count: 3,
    fell_open_count: 2,
    verification_rate: 0.6,
    most_common_reason: 'probe_passed',
    degraded_project_count: 1,
    project_count: 4,
    ...overrides,
  };
}

describe('MergeGateFleetHealthCard viewport', () => {
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
        root.render(<MergeGateFleetHealthCard data={makeData()} />);
      });
    });
  });

  test('renders without overflow at 768px (tablet)', async () => {
    await expectNoOverflowAt(768, async () => {
      act(() => {
        root.render(<MergeGateFleetHealthCard data={makeData()} />);
      });
    });
  });

  test('renders without overflow at 1024px (desktop)', async () => {
    await expectNoOverflowAt(1024, async () => {
      act(() => {
        root.render(<MergeGateFleetHealthCard data={makeData()} />);
      });
    });
  });
});
