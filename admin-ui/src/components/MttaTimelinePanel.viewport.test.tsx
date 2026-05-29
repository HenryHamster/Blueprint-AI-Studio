import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { MttaTimelinePanel } from './MttaTimelinePanel';
import { expectNoOverflowAt, expectNoTextTruncationAt, expectMinTouchTargetAt } from '../test/viewport';

vi.mock('../api', () => ({
  CANONICAL_LAYER_NAMES: ['CI_WORKFLOW', 'AGENT_VM', 'AGENT_VERB', 'BASELINE_GATE', 'REVIEWER_PASS', 'VIEWPORT'] as const,
  fetchCanonicalRegressionLayers: vi.fn(async () => ({ layers: [] })),
  apiFetch: vi.fn(async (url: string) => {
    if (url.includes('regression-dispatcher/mtta')) {
      return new Response(JSON.stringify({
        project_id: 'project-123',
        window_days: 30,
        class_count: 2,
        recovery_cooldown_sec: 900,
        classes: [
          {
            regression_class: 'sr_regression_pipeline_success_rate',
            sample_count: 5,
            stages: {
              signal_trip_to_detect: { p50_sec: 600, p90_sec: 1200, sample_count: 5 },
              detect_to_investigate: { p50_sec: 300, p90_sec: 600, sample_count: 4 },
              investigate_to_merge: { p50_sec: 3600, p90_sec: 7200, sample_count: 3 },
              merge_to_recovery: { p50_sec: 1800, p90_sec: 3600, sample_count: 2 },
            },
            active_cases: [
              {
                case_id: 'ac-1',
                status: 'INVESTIGATING',
                live_elapsed_sec: 1200,
                live_elapsed_investigate_sec: 600,
                recovery_check_in_sec: null,
                elapsed_since_merge_sec: null,
                eta_to_investigate_sec: null,
                eta_to_merge_sec: 3000,
                eta_to_recovery_sec: null,
              },
              {
                case_id: 'ac-2',
                status: 'MERGED',
                live_elapsed_sec: 5400,
                live_elapsed_investigate_sec: 3600,
                recovery_check_in_sec: 300,
                elapsed_since_merge_sec: 1800,
                eta_to_investigate_sec: null,
                eta_to_merge_sec: null,
                eta_to_recovery_sec: 0,
              },
            ],
          },
          {
            regression_class: 'fix_loop_divergence',
            sample_count: 3,
            stages: {
              signal_trip_to_detect: { p50_sec: 300, p90_sec: 600, sample_count: 3 },
              detect_to_investigate: { p50_sec: 200, p90_sec: 400, sample_count: 2 },
              investigate_to_merge: { p50_sec: 2400, p90_sec: 4800, sample_count: 2 },
              merge_to_recovery: { p50_sec: 900, p90_sec: 1800, sample_count: 1 },
            },
            active_cases: [
              {
                case_id: 'ac-3',
                status: 'INVESTIGATING',
                live_elapsed_sec: 400,
                live_elapsed_investigate_sec: 200,
                recovery_check_in_sec: null,
                elapsed_since_merge_sec: null,
                eta_to_investigate_sec: null,
                eta_to_merge_sec: 2200,
                eta_to_recovery_sec: null,
              },
            ],
          },
        ],
      }), { status: 200 });
    }
    return new Response('{}', { status: 404 });
  }),
}));

describe('MttaTimelinePanel — viewport regression with per-stage elapsed/ETA', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => { if (root) act(() => root?.unmount()); root = null; host?.remove(); host = null; });

  test('renders populated state without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<MttaTimelinePanel projectId="project-123" />); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    });
  });

  test('expands to show active cases with per-stage breakdown without overflow or truncation', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<MttaTimelinePanel projectId="project-123" />); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
      const button = host?.querySelector('[data-testid="mtta-timeline-panel"] button');
      if (button) button.dispatchEvent(new Event('click', { bubbles: true }));
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    });

    if (root) act(() => root?.unmount());
    root = null;
    host?.remove();
    host = null;

    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<MttaTimelinePanel projectId="project-123" />); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
      const button = host?.querySelector('[data-testid="mtta-timeline-panel"] button');
      if (button) button.dispatchEvent(new Event('click', { bubbles: true }));
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    }, { selector: '[data-testid="mtta-timeline-panel"] *' });
  });

  test('expand button meets the minimum touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<MttaTimelinePanel projectId="project-123" />); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    });
  });

  test('does not render when no active cases exist', async () => {
    const { apiFetch } = await import('../api');
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({
        project_id: 'project-123',
        window_days: 30,
        class_count: 0,
        recovery_cooldown_sec: 900,
        classes: [],
      }), { status: 200 })
    );
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => { root?.render(<MttaTimelinePanel projectId="project-123" />); });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    const panel = host.querySelector('[data-testid="mtta-timeline-panel"]');
    if (panel) throw new Error('panel should not render when no classes exist');
  });
});
