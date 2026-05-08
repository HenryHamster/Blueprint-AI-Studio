import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { RecentlyResolvedRegressionsPanel } from './RecentlyResolvedRegressionsPanel';
import { expectNoOverflowAt } from '../test/viewport';

// Mock '../api' so apiFetch returns a controlled cases payload (mirrors
// the pattern in RecentlyResolvedRegressionsPanel.test.tsx).
vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response(JSON.stringify({ cases: [] }), { status: 200 })),
  fetchCrossLayerCompliance: vi.fn(async () => ({
    total_classified: 0,
    compliant: 0,
    non_compliant: 0,
    awaiting_backfill_count: 0,
    compliance_rate: null,
    failure_modes: { doc_test_only: 0, most_recent_layer_only: 0 },
    by_class: {},
  })),
  authHeaders: vi.fn(async () => ({})),
  API_BASE_URL: 'http://localhost:8000',
}));

const populatedCases = [
  {
    id: 'case-1',
    regression_class: 'pipeline.timeout.evaluating-stuck-with-a-very-long-class-name-that-may-need-break-all',
    fix_pr_number: 4321,
    recovered_at: new Date(Date.now() - 600_000).toISOString(),
    first_seen_at: new Date(Date.now() - 1_800_000).toISOString(),
    signal_first_crossed_at: new Date(Date.now() - 2_400_000).toISOString(),
    time_to_recover_sec: 1200,
  },
  {
    id: 'case-2',
    regression_class: 'agent.heartbeat.lost',
    fix_pr_number: null,
    recovered_at: null,
    first_seen_at: '2026-05-05T00:00:00Z',
    signal_first_crossed_at: null,
    time_to_recover_sec: null,
  },
];

describe('RecentlyResolvedRegressionsPanel — viewport regression', () => {
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

  test('empty (no resolved cases) renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
      });
      // Component returns null until first fetch resolves; settle the load.
      await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
    });
  });

  test('populated cases render without horizontal overflow at 375px', async () => {
    const { apiFetch } = await import('../api');
    (apiFetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      new Response(JSON.stringify({ cases: populatedCases }), { status: 200 }),
    );
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
      });
      await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
    });
  });
});
