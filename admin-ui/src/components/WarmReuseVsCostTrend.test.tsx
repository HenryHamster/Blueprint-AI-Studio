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
  // Idle-inclusive cost companion: same time axis, higher values (idle overhead),
  // baselined against the pre-pool idle-exclusive cost (3.0).
  cost_per_merged_pr_incl_pool_idle_axis: {
    current: 3.9,
    baseline: 3.0,
    drift_pct: 30.0,
    history: [
      { captured_at: '2026-05-01T00:00:00', value: 3.4 },
      { captured_at: '2026-05-02T00:00:00', value: 3.6 },
      { captured_at: '2026-05-03T00:00:00', value: 3.9 },
    ],
  },
  // Truthful per-origin companion to the contaminated aggregate reuse line.
  warm_start_origin_breakdown: {
    warm_pool: 8,
    backlog_reuse: 2,
    chain_continuity: 6,
    warm_total: 16,
    pool_origin_share: 0.5,
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

  test('renders reuse + both cost series zipped onto a shared captured_at axis', async () => {
    axesPayload = POPULATED;
    ({ root, host } = await renderWidget());

    // Reuse, headline cost, and idle-inclusive cost lines render; the two cost
    // lines share the cost axis, reuse keeps its own.
    const reuseLine = lineProps.find((p) => p.dataKey === 'reuse');
    const costLine = lineProps.find((p) => p.dataKey === 'cost');
    const costInclLine = lineProps.find((p) => p.dataKey === 'costIncl');
    expect(reuseLine?.yAxisId).toBe('reuse');
    expect(costLine?.yAxisId).toBe('cost');
    expect(costInclLine?.yAxisId).toBe('cost');

    // The dataset is a single merged series sharing captured_at, now carrying
    // the idle-inclusive cost.
    const data = chartData as Array<{ captured_at: string; reuse: number | null; cost: number | null; costIncl: number | null }>;
    expect(data).toHaveLength(3);
    expect(data[0]).toEqual({ captured_at: '2026-05-01T00:00:00', reuse: 20, cost: 3.2, costIncl: 3.4 });
    expect(data[2]).toEqual({ captured_at: '2026-05-03T00:00:00', reuse: 42, cost: 3.1, costIncl: 3.9 });
  });

  test('draws reuse and cost baseline reference lines', async () => {
    axesPayload = POPULATED;
    ({ root, host } = await renderWidget());

    // One reuse baseline (reuse axis) + one cost baseline (cost axis).
    expect(referenceLineProps).toHaveLength(2);
    const reuseRef = referenceLineProps.find((p) => p.yAxisId === 'reuse');
    const costRef = referenceLineProps.find((p) => p.yAxisId === 'cost');
    expect(reuseRef?.y).toBe(18);
    // Cost baseline is the immutable pre-pool idle-exclusive cost (3.0).
    expect(costRef?.y).toBe(3.0);
  });

  test('shows the idle-inclusive cost drift readout', async () => {
    axesPayload = POPULATED;
    ({ root, host } = await renderWidget());
    expect(host.textContent).toContain('idle-incl cost +30.0% vs baseline');
  });

  test('omits the inclusive cost line and cost baseline when the axis is absent', async () => {
    axesPayload = { ...POPULATED, cost_per_merged_pr_incl_pool_idle_axis: undefined };
    ({ root, host } = await renderWidget());

    expect(lineProps.find((p) => p.dataKey === 'costIncl')).toBeUndefined();
    // Only the reuse baseline remains (cost baseline omitted with the axis).
    expect(referenceLineProps).toHaveLength(1);
    expect(referenceLineProps[0].yAxisId).toBe('reuse');
    expect(host.textContent).not.toContain('idle-incl cost');
  });

  test('omits the reuse baseline reference line when its baseline is null', async () => {
    axesPayload = {
      ...POPULATED,
      axes: {
        ...POPULATED.axes,
        vm_reuse_rate: { ...POPULATED.axes.vm_reuse_rate, baseline: null },
      },
    };
    ({ root, host } = await renderWidget());
    // Reuse baseline gone, but the cost baseline still renders independently.
    expect(referenceLineProps.find((p) => p.yAxisId === 'reuse')).toBeUndefined();
    expect(referenceLineProps.find((p) => p.yAxisId === 'cost')?.y).toBe(3.0);
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

  test('renders the warm-start origin breakdown and pool share beside the trend', async () => {
    axesPayload = POPULATED;
    ({ root, host } = await renderWidget());

    const row = host.querySelector('[data-testid="warm-start-origin-breakdown"]');
    expect(row).not.toBeNull();
    const share = host.querySelector('[data-testid="warm-start-pool-share"]');
    expect(share?.textContent).toContain('50%');
  });

  test('omits the origin breakdown when the response lacks it', async () => {
    axesPayload = { ...POPULATED, warm_start_origin_breakdown: undefined };
    ({ root, host } = await renderWidget());

    expect(host.querySelector('[data-testid="warm-start-origin-breakdown"]')).toBeNull();
    // The aggregate reuse trend still renders — only the companion is omitted.
    expect(lineProps.find((p) => p.dataKey === 'reuse')).toBeDefined();
  });

  test('renders nothing without a selected project', async () => {
    ({ root, host } = await renderWidget(null));
    expect(host.textContent).toBe('');
  });
});
