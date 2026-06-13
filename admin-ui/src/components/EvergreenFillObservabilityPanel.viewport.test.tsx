import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { EvergreenFillObservabilityPanel } from './EvergreenFillObservabilityPanel';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api');
  return {
    ...actual,
    fetchEvergreenFillObservability: vi.fn(async () => ({
      created_count: 7,
      reason_counts: {
        utilization_below_threshold: 5,
        manual_trigger: 2,
      },
      trigger_counts: {
        scheduled: 4,
        manual: 3,
      },
    })),
  };
});

describe('EvergreenFillObservabilityPanel — viewport regression', () => {
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
        root?.render(<EvergreenFillObservabilityPanel />);
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
        root?.render(<EvergreenFillObservabilityPanel />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    }, { waitMs: 50 });
  });
});
