import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { WarmReuseVsCostTrend } from './WarmReuseVsCostTrend';

// Capture the props passed to recharts primitives so we can assert on the
// merged dataset and the baseline reference line without a real DOM canvas.
const referenceLineProps: Array<Record<string, unknown>> = [];
const lineProps: Array<Record<string, unknown>> = [];
let chartData: unknown = null;

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  LineChart: ({ children, data }: { children: React.ReactNode; data: unknown }) => {
    chartData = data;
    return <div data-testid="chart">{children}</div>;
  },
  Line: (props: Record<string, unknown>) => {
    lineProps.push(props);
    return null;
  },
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  Legend: () => null,
  ReferenceLine: (props: Record<string, unknown>) => {
    referenceLineProps.push(props);
    return <div data-testid="reference-line" />;
  },
}));

let axesPayload: unknown = {};

vi.mock('../api', () => ({
  fetchEfficiencyAxes: vi.fn(async () => axesPayload),
}));

const POPULATED = {
  axes: {
    vm_reuse_rate: {
      current: 42,
      baseline: 18,
      drift_pct: 133.3,
      history: [
        { captured_at: '2026-05-01T00:00:00', value: 20 },
        { captured_at: '2026-05-02T00:00:00', value: 30 },
        { captured_at: '2026-05-03T00:00:00', value: 42 },
      ],
    },
    cost_per_merged_pr: {
      current: 3.1,
      baseline: 3.0,
      drift_pct: 3.3,
      history: [
        { captured_at: '2026-05-01T00:00:00', value: 3.2 },
        { captured_at: '2026-05-02T00:00:00', value: 3.0 },
        { captured_at: '2026-05-03T00:00:00', value: 3.1 },
      ],
    },
  },
  baseline_captured_at: '2026-04-24T00:00:00',
  last_snapshot_at: '2026-05-03T00:00:00',
};

async function renderWidget(projectId: string | null = 'proj-1'): Promise<{ root: Root; host: HTMLDivElement }> {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => {
    root.render(<WarmReuseVsCostTrend projectId={projectId} />);
  });
  await act(async () => {
    await new Promise<void>((resolve) => setTimeout(resolve, 40));
  });
  return { root, host };
}

describe('WarmReuseVsCostTrend', () => {
  let root: Root | null = null;
  let host: HTMLElement | null = null;

  afterEach(() => {
    if (root) {
      const r = root;
      act(() => r.unmount());
      root = null;
    }
    host?.remove();
    host = null;
    referenceLineProps.length = 0;
    lineProps.length = 0;
    chartData = null;
    axesPayload = {};
  });

  test('renders both series zipped onto a shared captured_at axis', async () => {
    axesPayload = POPULATED;
    ({ root, host } = await renderWidget());

    // Both reuse and cost lines are rendered, each pinned to its own Y axis.
    const reuseLine = lineProps.find((p) => p.dataKey === 'reuse');
    const costLine = lineProps.find((p) => p.dataKey === 'cost');
    expect(reuseLine?.yAxisId).toBe('reuse');
    expect(costLine?.yAxisId).toBe('cost');

    // The dataset is a single merged series sharing captured_at.
    const data = chartData as Array<{ captured_at: string; reuse: number | null; cost: number | null }>;
    expect(data).toHaveLength(3);
    expect(data[0]).toEqual({ captured_at: '2026-05-01T00:00:00', reuse: 20, cost: 3.2 });
    expect(data[2]).toEqual({ captured_at: '2026-05-03T00:00:00', reuse: 42, cost: 3.1 });
  });

  test('draws the baseline reference line from vm_reuse_rate.baseline', async () => {
    axesPayload = POPULATED;
    ({ root, host } = await renderWidget());

    expect(referenceLineProps).toHaveLength(1);
    expect(referenceLineProps[0].y).toBe(18);
    expect(referenceLineProps[0].yAxisId).toBe('reuse');
  });

  test('omits the baseline reference line when baseline is null', async () => {
    axesPayload = {
      ...POPULATED,
      axes: {
        ...POPULATED.axes,
        vm_reuse_rate: { ...POPULATED.axes.vm_reuse_rate, baseline: null },
      },
    };
    ({ root, host } = await renderWidget());
    expect(referenceLineProps).toHaveLength(0);
  });

  test('shows baseline-pending state when history is too short', async () => {
    axesPayload = {
      axes: {
        vm_reuse_rate: { current: 10, baseline: null, drift_pct: null, history: [{ captured_at: '2026-05-01T00:00:00', value: 10 }] },
        cost_per_merged_pr: { current: 3, baseline: null, drift_pct: null, history: [] },
      },
      baseline_captured_at: null,
      last_snapshot_at: null,
    };
    ({ root, host } = await renderWidget());
    expect(host.textContent).toContain('Baseline pending');
  });

  test('renders nothing without a selected project', async () => {
    ({ root, host } = await renderWidget(null));
    expect(host.textContent).toBe('');
  });
});
