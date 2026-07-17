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
      auto_resolved_total: 3,
      auto_resolved_backing_path_total: 2,
      auto_resolved_named_test_present_total: 1,
      last_tick_at: '2026-07-06T10:00:00Z',
      last_tick: null,
      recent_ticks: [],
      recent_flags: [
        {
          check: 'capability_proof_gate_gap',
          status: 'flagged',
          self_reported: 8,
          ground_truth: 5,
          delta: -3,
          severity: 'warn',
          subject: 'proj-xyz',
          detail: '3 capabilities have no proof',
          auto_resolved: 2,
          auto_resolved_backing_path: 1,
          auto_resolved_named_test_present: 1,
          at: '2026-07-06T10:00:00Z',
        },
        {
          check: 'metrics_drift',
          status: 'flagged',
          self_reported: 12,
          ground_truth: 15,
          delta: 3,
          severity: 'error',
          subject: 'global',
          detail: 'fix loop iterations above threshold',
          auto_resolved: null,
          auto_resolved_backing_path: null,
          auto_resolved_named_test_present: null,
          at: '2026-07-06T10:00:00Z',
        },
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
