import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { EscalationOutcomePanel, FirstAttemptComplianceTrendPanel } from './PipelineRegressionsCompliancePanels';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async (url: string) => {
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
    return new Response('{}', { status: 404 });
  }),
}));

describe('EscalationOutcomePanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => { if (root) act(() => root?.unmount()); root = null; host?.remove(); host = null; });

  test('renders populated buckets without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<EscalationOutcomePanel projectId="project-123" />); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    });
  });

  test('mobile cards render all buckets without text truncation at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<EscalationOutcomePanel projectId="project-123" />); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    }, { waitMs: 50 });
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

  test('mobile cards render window labels without text truncation at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<FirstAttemptComplianceTrendPanel projectId="project-123" />); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    }, { waitMs: 50 });
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
