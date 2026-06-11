import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act, type ReactNode } from 'react';
import { FixLoopConvergenceWidget } from './FixLoopConvergenceWidget';
import { apiFetch } from '../api';

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  AreaChart: ({ children }: { children: ReactNode }) => <div>{children}</div>,
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
            { date: '2026-05-01', lint: { task_count: 1, convergence_rate: 1, divergence_rate: 0 }, test: { task_count: 1, convergence_rate: 1, divergence_rate: 0 }, check: { task_count: 1, convergence_rate: 1, divergence_rate: 0 } },
            { date: '2026-05-02', lint: { task_count: 1, convergence_rate: 1, divergence_rate: 0 }, test: { task_count: 1, convergence_rate: 1, divergence_rate: 0 }, check: { task_count: 1, convergence_rate: 1, divergence_rate: 0 } },
          ],
        }),
        { status: 200 },
      );
    }
    if (url.includes('/fix-loop-diverged-tasks')) {
      return new Response(JSON.stringify({ tasks: [], total: 0 }), { status: 200 });
    }
    if (url.includes('/fix-loop-convergence')) {
      return new Response(
        JSON.stringify({
          window_hours: 24,
          tasks_scanned: 4,
          tasks_with_fix_loops: 1,
          lint: {
            task_count: 1,
            avg_iterations: 2,
            convergence_rate: 0,
            exit_reason_counts: { divergence_oscillation: 1 },
            iteration_distribution: { '1': 0, '2': 1, '3': 0, '4+': 0 },
          },
          test: {
            task_count: 0,
            avg_iterations: null,
            convergence_rate: null,
            exit_reason_counts: {},
            iteration_distribution: { '1': 0, '2': 0, '3': 0, '4+': 0 },
          },
          check: {
            task_count: 0,
            avg_iterations: null,
            convergence_rate: null,
            exit_reason_counts: {},
            iteration_distribution: { '1': 0, '2': 0, '3': 0, '4+': 0 },
          },
        }),
        { status: 200 },
      );
    }
    return new Response('{}', { status: 200 });
  }),
}));

const apiFetchMock = vi.mocked(apiFetch);

async function flushWidget(): Promise<void> {
  await act(async () => {
    await new Promise<void>((resolve) => setTimeout(resolve, 80));
  });
}

function getRequestUrl(path: string, projectId: string): URL {
  const request = apiFetchMock.mock.calls.find(([url]) => (
    String(url).includes(path) && String(url).includes(`project_id=${projectId}`)
  ));
  expect(request).toBeDefined();
  return new URL(String(request?.[0]), 'http://localhost');
}

describe('FixLoopConvergenceWidget', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    apiFetchMock.mockClear();
    if (root) {
      const r = root;
      act(() => r.unmount());
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('passes_project_id_to_aggregate_trend_and_drilldown_requests', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);

    await act(async () => {
      root?.render(<FixLoopConvergenceWidget projectId="project-1" trendDays={14} />);
    });
    await flushWidget();

    const aggregateUrl = getRequestUrl('/api/admin/fix-loop-convergence', 'project-1');
    expect(aggregateUrl.searchParams.get('hours')).toBe('24');
    expect(aggregateUrl.searchParams.get('project_id')).toBe('project-1');

    const trendUrl = getRequestUrl('/api/admin/fix-loop-convergence-trend', 'project-1');
    expect(trendUrl.searchParams.get('days')).toBe('14');
    expect(trendUrl.searchParams.get('project_id')).toBe('project-1');

    const chip = Array.from(host.querySelectorAll('button')).find(button => (
      button.textContent?.includes('divergence oscillation')
    ));
    expect(chip).toBeDefined();
    await act(async () => {
      chip?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    await flushWidget();

    const drilldownUrl = getRequestUrl('/api/admin/fix-loop-diverged-tasks', 'project-1');
    expect(drilldownUrl.searchParams.get('hours')).toBe('24');
    expect(drilldownUrl.searchParams.get('loop')).toBe('lint');
    expect(drilldownUrl.searchParams.get('exit_reason')).toBe('divergence_oscillation');
    expect(drilldownUrl.searchParams.get('project_id')).toBe('project-1');
  });

  test('refetches aggregate and trend when project changes', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);

    await act(async () => {
      root?.render(<FixLoopConvergenceWidget projectId="project-1" />);
    });
    await flushWidget();

    apiFetchMock.mockClear();

    await act(async () => {
      root?.render(<FixLoopConvergenceWidget projectId="project-2" />);
    });
    await flushWidget();

    const aggregateUrl = getRequestUrl('/api/admin/fix-loop-convergence', 'project-2');
    expect(aggregateUrl.searchParams.get('project_id')).toBe('project-2');

    const trendUrl = getRequestUrl('/api/admin/fix-loop-convergence-trend', 'project-2');
    expect(trendUrl.searchParams.get('project_id')).toBe('project-2');
  });
});
