import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { DispatcherHealthPanel, LayerCoverageHistogramPanel, ManualInterventionPanel, RegressionRateTrendPanel, LayerDefenseDensityPanel, EscalationOutcomePanel, FirstAttemptComplianceTrendPanel, ClassKpisPanel } from './PipelineRegressionsPanel';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';

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
  CANONICAL_LAYER_NAMES: ['CI_WORKFLOW', 'AGENT_VM', 'AGENT_VERB', 'BASELINE_GATE', 'REVIEWER_PASS', 'VIEWPORT'] as const,
  fetchCanonicalRegressionLayers: vi.fn(async () => ({ layers: [] })),
  apiFetch: vi.fn(async (url: string) => {
    if (url.includes('regression-dispatcher/health')) {
      return new Response(JSON.stringify({
        window_hours: 24,
        aggregate: {
          cycles: 47,
          signals_evaluated: 50,
          signals_tripped: 12,
          spawns: 4,
          mttd_p50_sec: 150.5,
          recoveries: {
            marginal: 2,
            confirmed: 3,
          },
          rejections: {
            cooldown: 8,
            dedup: 1,
            system_cap: 0,
            manual_intervention_suppressed: 0,
          },
        },
        spawns_sparkline: Array.from({ length: 24 }, (_, i) => ({
          hour_start: new Date(Date.now() - (23 - i) * 3600_000).toISOString(),
          spawns: Math.floor(Math.random() * 5),
          cycles: Math.floor(Math.random() * 10),
        })),
        per_class: [
          {
            project_id: 'project-123',
            regression_class: 'sr_regression_pipeline_success_rate',
            status: 'ACTIVE',
            last_seen_at: new Date(Date.now() - 3600_000).toISOString(),
            attempts: 2,
            in_cooldown_until: new Date(Date.now() + 3600_000).toISOString(),
          },
          {
            project_id: 'project-123',
            regression_class: 'sr_regression_first_attempt_success',
            status: 'ACTIVE',
            last_seen_at: new Date(Date.now() - 7200_000).toISOString(),
            attempts: 1,
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
          { layer: 'CI_WORKFLOW', fix_count: 8, last_fix_at: new Date(Date.now() - 86400_000).toISOString(), sample_case_ids: ['case-1', 'case-2'], avg_hold_days: 12.3, still_holding_count: 2, held_gt_7d_count: 3, retripped_within_48h_count: 0 },
          { layer: 'AGENT_VM', fix_count: 0, last_fix_at: null, sample_case_ids: [], avg_hold_days: null, still_holding_count: 0, held_gt_7d_count: 0, retripped_within_48h_count: 0 },
          { layer: 'AGENT_VERB', fix_count: 3, last_fix_at: new Date(Date.now() - 172800_000).toISOString(), sample_case_ids: ['case-3'], avg_hold_days: 4.1, still_holding_count: 0, held_gt_7d_count: 0, retripped_within_48h_count: 1 },
          { layer: 'BASELINE_GATE', fix_count: 1, last_fix_at: new Date(Date.now() - 604800_000).toISOString(), sample_case_ids: ['case-4'], avg_hold_days: null, still_holding_count: 1, held_gt_7d_count: 1, retripped_within_48h_count: 0 },
          { layer: 'REVIEWER_PASS', fix_count: 5, last_fix_at: new Date(Date.now() - 43200_000).toISOString(), sample_case_ids: ['case-5'], avg_hold_days: 2.5, still_holding_count: 0, held_gt_7d_count: 0, retripped_within_48h_count: 3 },
        ],
      }), { status: 200 });
    }
    if (url.includes('regression-class-kpis')) {
      return new Response(JSON.stringify({
        window_hours: 168,
        class_count: 3,
        classes: [
          { regression_class: 'sr_regression_pipeline_success_rate', cases_opened: 2, cases_closed: 1, dispatch_attempt_count: 3, remediation_success_rate: 0.67, mean_time_to_detect_seconds: 3600, mean_time_to_remediate_seconds: 7200 },
          { regression_class: 'fix_loop_divergence', cases_opened: 5, cases_closed: 3, dispatch_attempt_count: 8, remediation_success_rate: 0.4, mean_time_to_detect_seconds: 1800, mean_time_to_remediate_seconds: 14400 },
          { regression_class: 'viewport_regression:MessagesPanel@375', cases_opened: 1, cases_closed: 1, dispatch_attempt_count: 1, remediation_success_rate: 1.0, mean_time_to_detect_seconds: 900, mean_time_to_remediate_seconds: 3600 },
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
    if (url.includes('escalation-outcomes')) {
      return new Response(JSON.stringify({
        by_reason: {
          escalated_cross_layer: { merged_count: 3, retripped_within_30d_count: 1, durable_resolution_rate: 0.67, median_hold_days: 12.5, in_flight_excluded_count: 0 },
          compliance_followup: { merged_count: 2, retripped_within_30d_count: 0, durable_resolution_rate: 1.0, median_hold_days: 18.3, in_flight_excluded_count: 0, prevention_rate_30d: 0.85 },
          escalated_layer_concentration: { merged_count: 2, retripped_within_30d_count: 0, durable_resolution_rate: 1.0, median_hold_days: 8.0, in_flight_excluded_count: 0 },
          escalated_cross_case_fingerprint: { merged_count: 1, retripped_within_30d_count: 0, durable_resolution_rate: 1.0, median_hold_days: null, in_flight_excluded_count: 0 },
          escalated_recurrence: { merged_count: 0, retripped_within_30d_count: 0, durable_resolution_rate: null, median_hold_days: null, in_flight_excluded_count: 1 },
          all_other: { merged_count: 15, retripped_within_30d_count: 3, durable_resolution_rate: 0.8, median_hold_days: 5.2, in_flight_excluded_count: 2 },
        },
        lookback_days: 30,
        generated_at: new Date().toISOString(),
      }), { status: 200 });
    }
    if (url.includes('first-attempt-compliance-trend')) {
      const now = new Date('2026-05-21T00:00:00Z');
      return new Response(JSON.stringify({
        windows: Array.from({ length: 6 }, (_, i) => {
          const end = new Date(now);
          end.setDate(end.getDate() - (5 - i) * 7);
          const start = new Date(end);
          start.setDate(start.getDate() - 7);
          return {
            window_start: start.toISOString(),
            window_end: end.toISOString(),
            first_attempt_compliant: i < 2 ? 0 : i,
            followup_rescued: i < 2 ? 0 : Math.max(0, 2 - i),
            awaiting_backfill: i === 5 ? 1 : 0,
            rate: i < 2 ? null : i / (i + Math.max(0, 2 - i)),
          };
        }),
        num_windows: 6,
        window_days: 7,
        generated_at: now.toISOString(),
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
        window_hours: 24,
        aggregate: {
          cycles: 0,
          signals_evaluated: 0,
          signals_tripped: 0,
          spawns: 0,
          mttd_p50_sec: null,
          recoveries: {
            marginal: 0,
            confirmed: 0,
          },
          rejections: {
            cooldown: 0,
            dedup: 0,
            system_cap: 0,
            manual_intervention_suppressed: 0,
          },
        },
        spawns_sparkline: Array.from({ length: 24 }, (_, i) => ({
          hour_start: new Date(Date.now() - (23 - i) * 3600_000).toISOString(),
          spawns: 0,
          cycles: 0,
        })),
        per_class: [],
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
  test('ManualInterventionPanel action chips meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<ManualInterventionPanel projectId="project-123" />); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    }, { selector: 'button[title]', waitMs: 50 });
  });
  test('renders long regression class labels without silent text clipping at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<ManualInterventionPanel projectId="project-123" />); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    }, { waitMs: 50 });
  });
});

describe('ClassKpisPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => { if (root) act(() => root?.unmount()); root = null; host?.remove(); host = null; });
  test('renders populated per-class KPIs table without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<ClassKpisPanel projectId="project-123" />); });
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
          { layer: 'CI_WORKFLOW', fix_count: 0, last_fix_at: null, sample_case_ids: [], avg_hold_days: null, still_holding_count: 0, held_gt_7d_count: 0, retripped_within_48h_count: 0 },
          { layer: 'AGENT_VM', fix_count: 0, last_fix_at: null, sample_case_ids: [], avg_hold_days: null, still_holding_count: 0, held_gt_7d_count: 0, retripped_within_48h_count: 0 },
          { layer: 'AGENT_VERB', fix_count: 0, last_fix_at: null, sample_case_ids: [], avg_hold_days: null, still_holding_count: 0, held_gt_7d_count: 0, retripped_within_48h_count: 0 },
          { layer: 'BASELINE_GATE', fix_count: 0, last_fix_at: null, sample_case_ids: [], avg_hold_days: null, still_holding_count: 0, held_gt_7d_count: 0, retripped_within_48h_count: 0 },
          { layer: 'REVIEWER_PASS', fix_count: 0, last_fix_at: null, sample_case_ids: [], avg_hold_days: null, still_holding_count: 0, held_gt_7d_count: 0, retripped_within_48h_count: 0 },
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

  test('renders fragile layers (avg_hold_days < 7 or retripped >= 2) without overflow at 375px', async () => {
    const { apiFetch } = await import('../api');
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({
        days: 30,
        generated_at: new Date().toISOString(),
        layers: [
          { layer: 'CI_WORKFLOW', fix_count: 4, last_fix_at: new Date(Date.now() - 86400_000).toISOString(), sample_case_ids: ['c1'], avg_hold_days: 1.2, still_holding_count: 0, held_gt_7d_count: 0, retripped_within_48h_count: 3 },
          { layer: 'AGENT_VM', fix_count: 2, last_fix_at: new Date(Date.now() - 43200_000).toISOString(), sample_case_ids: ['c2'], avg_hold_days: 0.5, still_holding_count: 0, held_gt_7d_count: 0, retripped_within_48h_count: 2 },
          { layer: 'AGENT_VERB', fix_count: 0, last_fix_at: null, sample_case_ids: [], avg_hold_days: null, still_holding_count: 0, held_gt_7d_count: 0, retripped_within_48h_count: 0 },
          { layer: 'BASELINE_GATE', fix_count: 1, last_fix_at: new Date(Date.now() - 172800_000).toISOString(), sample_case_ids: ['c3'], avg_hold_days: 14.0, still_holding_count: 0, held_gt_7d_count: 1, retripped_within_48h_count: 0 },
          { layer: 'REVIEWER_PASS', fix_count: 3, last_fix_at: new Date(Date.now() - 3600_000).toISOString(), sample_case_ids: ['c4'], avg_hold_days: null, still_holding_count: 3, held_gt_7d_count: 2, retripped_within_48h_count: 0 },
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

describe('EscalationOutcomePanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => { if (root) act(() => root?.unmount()); root = null; host?.remove(); host = null; });
  test('renders populated state without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<EscalationOutcomePanel projectId="project-123" />); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    });
  });
  test('does not render when all merged_count are zero', async () => {
    const { apiFetch } = await import('../api');
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({
        by_reason: {
          escalated_cross_layer: { merged_count: 0, retripped_within_30d_count: 0, durable_resolution_rate: null, median_hold_days: null, in_flight_excluded_count: 0 },
          all_other: { merged_count: 0, retripped_within_30d_count: 0, durable_resolution_rate: null, median_hold_days: null, in_flight_excluded_count: 0 },
        },
        lookback_days: 30,
        generated_at: new Date().toISOString(),
      }), { status: 200 })
    );
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => { root?.render(<EscalationOutcomePanel projectId="project-123" />); });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    const panel = host.querySelector('[data-testid="escalation-outcome-panel"]');
    if (panel) throw new Error('panel should not render when all merged_count are zero');
  });
});

describe('FirstAttemptComplianceTrendPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => { if (root) act(() => root?.unmount()); root = null; host?.remove(); host = null; });
  test('renders populated trend without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<FirstAttemptComplianceTrendPanel projectId="project-123" />); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    });
  });
  test('does not render when all windows are empty', async () => {
    const { apiFetch } = await import('../api');
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({
        windows: Array.from({ length: 6 }, (_, i) => ({
          window_start: new Date(Date.now() - (6 - i) * 7 * 86400_000).toISOString(),
          window_end: new Date(Date.now() - (5 - i) * 7 * 86400_000).toISOString(),
          first_attempt_compliant: 0,
          followup_rescued: 0,
          awaiting_backfill: 0,
          rate: null,
        })),
        num_windows: 6,
        window_days: 7,
        generated_at: new Date().toISOString(),
      }), { status: 200 })
    );
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => { root?.render(<FirstAttemptComplianceTrendPanel projectId="project-123" />); });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    const panel = host.querySelector('[data-testid="first-attempt-compliance-trend-panel"]');
    if (panel) throw new Error('panel should not render when all windows are empty');
  });
});

describe('ManualInterventionPanel — viewport_regression baseline disclosure', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => { if (root) act(() => root?.unmount()); root = null; host?.remove(); host = null; });
  test('renders viewport_regression case with baseline disclosure without overflow at 375px', async () => {
    const { apiFetch } = await import('../api');
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.includes('/manual-intervention')) {
        return new Response(JSON.stringify({ cases: [{
          id: 'vp-case-1', regression_class: 'viewport_regression:MessagesPanel@375',
          signal_name: 'viewport_regression', status: 'NEEDS_ATTENTION', attempts: 2,
          last_remediation_pr_number: null, last_failure_class: null,
          signal_snapshot_at_spawn: null, signal_snapshot_at_merge: null,
          fingerprint_to_fix_log: [],
          last_seen_at: '2026-05-09T10:00:00Z', needs_manual_intervention_at: '2026-05-09T10:00:00Z',
          first_seen_at: '2026-05-09T09:00:00Z', signal_first_crossed_at: '2026-05-09T09:00:00Z',
          time_to_detect_sec: 3600,
        }] }), { status: 200 });
      }
      return new Response('{}', { status: 404 });
    });
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
