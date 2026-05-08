import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { DispatcherHealthPanel, LayerCoverageHistogramPanel, ManualInterventionPanel, RegressionRateTrendPanel, LayerDefenseDensityPanel } from './PipelineRegressionsPanel';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  LineChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Line: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
}));

vi.mock('../api', () => ({
  apiFetch: vi.fn(async (url: string) => {
    if (url.includes('regression-dispatcher/health')) {
      return new Response(JSON.stringify({
        cycles_last_24h: 47,
        signals_tripped_last_24h: 12,
        spawns_last_24h: 4,
        cooldown_blocks_last_24h: 8,
        cap_blocks_last_24h: 0,
        classes: [
          {
            regression_class: 'sr_regression_pipeline_success_rate',
            last_spawn_at: new Date(Date.now() - 3600_000).toISOString(),
            last_rejection_reason: null,
            in_cooldown_until: new Date(Date.now() + 3600_000).toISOString(),
          },
          {
            regression_class: 'sr_regression_first_attempt_success',
            last_spawn_at: new Date(Date.now() - 7200_000).toISOString(),
            last_rejection_reason: 'cooldown',
            in_cooldown_until: null,
          },
        ],
      }), { status: 200 });
    }
    if (url.includes('regression-cases/trend')) {
      const daily = Array.from({ length: 30 }, (_, i) => {
        const d = new Date('2026-04-09');
        d.setDate(d.getDate() + i);
        return { date: d.toISOString().slice(0, 10), opened: Math.floor(Math.random() * 5), closed: Math.floor(Math.random() * 4) };
      });
      return new Response(JSON.stringify({ project_id: 'project-123', days: 30, daily, by_class: {} }), { status: 200 });
    }
    if (url.includes('layer-defense-density')) {
      return new Response(JSON.stringify({
        days: 30,
        generated_at: new Date().toISOString(),
        layers: [
          { layer: 'CI_WORKFLOW', fix_count: 8, last_fix_at: new Date(Date.now() - 86400_000).toISOString(), sample_case_ids: ['case-1', 'case-2'] },
          { layer: 'AGENT_VM', fix_count: 0, last_fix_at: null, sample_case_ids: [] },
          { layer: 'AGENT_VERB', fix_count: 3, last_fix_at: new Date(Date.now() - 172800_000).toISOString(), sample_case_ids: ['case-3'] },
          { layer: 'BASELINE_GATE', fix_count: 1, last_fix_at: new Date(Date.now() - 604800_000).toISOString(), sample_case_ids: ['case-4'] },
          { layer: 'REVIEWER_PASS', fix_count: 5, last_fix_at: new Date(Date.now() - 43200_000).toISOString(), sample_case_ids: ['case-5'] },
        ],
      }), { status: 200 });
    }
    if (url.includes('regression-cases') && !url.includes('manual-intervention') && !url.includes('auto-recovered') && !url.includes('trend')) {
      return new Response(JSON.stringify({
        count: 2,
        status_filter: ['ACTIVE'],
        summary: {
          by_regression_class: [],
          layer_coverage_by_class: {
            sr_regression_pipeline_success_rate: {
              total_fixes_with_layers: 5,
              combinations: [
                { layers: ['AGENT_VM'], count: 3 },
                { layers: ['AGENT_VM', 'CI_WORKFLOW'], count: 2 },
              ],
            },
            long_running_class_name_with_no_canonical_layer_targeted_ever: {
              total_fixes_with_layers: 2,
              combinations: [
                { layers: [], count: 2 },
              ],
            },
          },
        },
        cases: [],
      }), { status: 200 });
    }
    if (url.includes('/manual-intervention')) {
      return new Response(JSON.stringify({ cases: Array.from({ length: 6 }, (_, i) => ({
        id: `case-${i + 1}`, regression_class: `very_long_regression_class_name_${i + 1}_with_nested_failure_signals`,
        signal_name: `signal-${i + 1}`, status: 'NEEDS_ATTENTION', attempts: i + 2, last_remediation_pr_number: 1200 + i,
        last_failure_class: `LONG_FAILURE_CLASS_NAME_${i + 1}_WITH_MULTIPLE_SEGMENTS`,
        signal_snapshot_at_spawn: { task_type: 'create', severity: 'critical', queue_depth: 18 + i, note: 'initial snapshot has enough detail to pressure the card layout' },
        signal_snapshot_at_merge: { task_type: 'create', severity: 'critical', queue_depth: 24 + i, note: 'merge snapshot stays dense enough to exercise wrapping' },
        fingerprint_to_fix_log: [{ fix_pr_number: 700 + i, merged_at: '2026-05-06T00:00:00Z', recovered_at: i % 2 === 0 ? '2026-05-06T01:00:00Z' : undefined }],
        last_seen_at: '2026-05-06T02:00:00Z', needs_manual_intervention_at: '2026-05-06T03:00:00Z',
        first_seen_at: '2026-05-06T01:05:00Z', signal_first_crossed_at: '2026-05-06T01:00:00Z',
      })) }), { status: 200 });
    }
    return new Response('{}', { status: 404 });
  }),
}));

describe('DispatcherHealthPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('renders populated state without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<DispatcherHealthPanel projectId="project-test" />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    });
  });

  test('renders empty state without overflow at 375px', async () => {
    const { apiFetch } = await import('../api');
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({
        cycles_last_24h: 0,
        signals_tripped_last_24h: 0,
        spawns_last_24h: 0,
        cooldown_blocks_last_24h: 0,
        cap_blocks_last_24h: 0,
        classes: [],
      }), { status: 200 })
    );
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<DispatcherHealthPanel projectId="project-test" />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    });
  });
});

describe('ManualInterventionPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => { if (root) act(() => root?.unmount()); root = null; host?.remove(); host = null; });
  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<ManualInterventionPanel projectId="project-123" />); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    });
  });
});

describe('LayerCoverageHistogramPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => { if (root) act(() => root?.unmount()); root = null; host?.remove(); host = null; });
  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<LayerCoverageHistogramPanel projectId="project-123" />); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    });
  });
});

describe('RegressionRateTrendPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => { if (root) act(() => root?.unmount()); root = null; host?.remove(); host = null; });
  test('renders trend chart without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<RegressionRateTrendPanel projectId="project-123" />); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    });
  });
});

describe('LayerDefenseDensityPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => { if (root) act(() => root?.unmount()); root = null; host?.remove(); host = null; });
  test('renders populated state without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<LayerDefenseDensityPanel projectId="project-123" />); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    });
  });
  test('renders empty state without overflow at 375px', async () => {
    const { apiFetch } = await import('../api');
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({
        days: 30,
        generated_at: new Date().toISOString(),
        layers: [
          { layer: 'CI_WORKFLOW', fix_count: 0, last_fix_at: null, sample_case_ids: [] },
          { layer: 'AGENT_VM', fix_count: 0, last_fix_at: null, sample_case_ids: [] },
          { layer: 'AGENT_VERB', fix_count: 0, last_fix_at: null, sample_case_ids: [] },
          { layer: 'BASELINE_GATE', fix_count: 0, last_fix_at: null, sample_case_ids: [] },
          { layer: 'REVIEWER_PASS', fix_count: 0, last_fix_at: null, sample_case_ids: [] },
        ],
      }), { status: 200 })
    );
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<LayerDefenseDensityPanel projectId="project-123" />); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    });
  });
});
