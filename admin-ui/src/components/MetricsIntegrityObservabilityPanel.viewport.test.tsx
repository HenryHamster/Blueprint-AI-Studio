import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { MetricsIntegrityObservabilityPanel } from './MetricsIntegrityObservabilityPanel';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api');
  return {
    ...actual,
    fetchMetricsIntegrityObservability: vi.fn(async () => ({
      ticks: 12,
      checks_run: 36,
      flags_total: 2,
      errors_total: 0,
      enrolled_total: 4,
      last_tick_at: '2026-07-06T10:00:00Z',
      last_tick: null,
      recent_ticks: [],
      recent_flags: [
        { check_id: 'metric-drift-1', metric: 'task_completion_rate', expected: '> 0.85', found: '0.72' },
        { check_id: 'metric-drift-2', metric: 'avg_fix_loop_iterations', expected: '< 3.5', found: '4.1' },
      ],
    })),
  };
});

describe('MetricsIntegrityObservabilityPanel — viewport regression', () => {
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
        root?.render(<MetricsIntegrityObservabilityPanel />);
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
        root?.render(<MetricsIntegrityObservabilityPanel />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    }, { waitMs: 50 });
  });
});
