import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { FixLoopConvergenceWidget } from './FixLoopConvergenceWidget';
import { expectNoOverflowAt, expectMinTouchTargetAt } from '../test/viewport';

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  AreaChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Area: () => null,
  Tooltip: () => null,
}));

vi.mock('../api', () => ({
  apiFetch: vi.fn(async (url: string) => {
    if (url.includes('/fix-loop-convergence-trend')) {
      return new Response(
        JSON.stringify({
          days: 7,
          buckets: [
            { date: '2026-05-01', lint: { task_count: 10, convergence_rate: 0.7, divergence_rate: 0.1 }, test: { task_count: 8, convergence_rate: 0.5, divergence_rate: 0.2 }, check: { task_count: 12, convergence_rate: 0.85, divergence_rate: 0.05 } },
            { date: '2026-05-02', lint: { task_count: 11, convergence_rate: 0.65, divergence_rate: 0.1 }, test: { task_count: 9, convergence_rate: 0.55, divergence_rate: 0.15 }, check: { task_count: 14, convergence_rate: 0.8, divergence_rate: 0.06 } },
            { date: '2026-05-03', lint: { task_count: 12, convergence_rate: 0.72, divergence_rate: 0.08 }, test: { task_count: 10, convergence_rate: 0.6, divergence_rate: 0.18 }, check: { task_count: 13, convergence_rate: 0.78, divergence_rate: 0.07 } },
          ],
        }),
        { status: 200 },
      );
    }
    if (url.includes('/fix-loop-convergence')) {
      return new Response(
        JSON.stringify({
          window_hours: 24,
          tasks_scanned: 142,
          tasks_with_fix_loops: 38,
          lint: {
            task_count: 12,
            avg_iterations: 1.4,
            convergence_rate: 0.72,
            exit_reason_counts: { converged: 8, divergence_oscillation: 2, max_iterations_exceeded: 2 },
            iteration_distribution: { '1': 5, '2': 4, '3': 2, '4+': 1 },
          },
          test: {
            task_count: 14,
            avg_iterations: 2.6,
            convergence_rate: 0.43,
            exit_reason_counts: { converged: 6, divergence_similar_error: 4, divergence_diff_revert: 2, max_iterations_exceeded: 2 },
            iteration_distribution: { '1': 3, '2': 4, '3': 4, '4+': 3 },
          },
          check: {
            task_count: 12,
            avg_iterations: 1.1,
            convergence_rate: 0.92,
            exit_reason_counts: { converged: 11, max_iterations_exceeded: 1 },
            iteration_distribution: { '1': 9, '2': 2, '3': 1, '4+': 0 },
          },
        }),
        { status: 200 },
      );
    }
    return new Response('{}', { status: 200 });
  }),
}));

describe('FixLoopConvergenceWidget — viewport regression', () => {
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

  test('renders 3-column loop grid + trend without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<FixLoopConvergenceWidget />);
      });
      // Wait for fetch + state update.
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 80));
      });
    });
  });

  test('renders 3-column loop grid + trend without horizontal overflow at 768px', async () => {
    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<FixLoopConvergenceWidget />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 80));
      });
    });
  });

  // Pre-existing dense exit-reason chip buttons in this widget are
  // intentionally below 44px and carry `data-viewport-allow-small-target`
  // attributes (the eslint-disable comments in the source document the
  // design intent). The helper skips opted-out elements, so this test
  // catches *new* buttons that lack the attribute — i.e. a regression
  // that adds a non-opted-out small button anywhere in the widget.
  test('has no undocumented small touch targets at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<FixLoopConvergenceWidget />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 80));
      });
    });
  });
});
