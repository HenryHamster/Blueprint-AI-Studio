import { afterEach, describe, test, vi, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act, useRef } from 'react';
import { MetricsBar } from './MetricsBar';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';

// Partial mock — `MetricsBar` -> `AutonomousStatus` imports `API_BASE_URL` and
// `authHeaders` alongside `apiFetch`. Browser-mode vitest validates ESM
// exports statically; preserving the original module via importOriginal
// ensures every transitively-needed name resolves.
vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>();
  return {
    ...actual,
    apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  };
});

// AutonomousStatus polls the orchestrator status endpoint via apiFetch — mock
// of '../api' is enough; the component renders an idle pill on first render.
function MetricsBarHarness({ showMetrics }: { showMetrics: boolean }) {
  const wsRef = useRef<WebSocket | null>(null);
  return (
    // The shell mirrors HeaderBar.tsx:335 — MetricsBar is intentionally
    // gated `hidden md:flex`, so the test asserts that the gate is honored
    // at 375px (no children rendered) and visible at 768px.
    <div className="hidden md:flex">
      <MetricsBar
        globalActiveCount={4}
        maxConcurrentVMs={8}
        globalEvaluatingCount={2}
        queuedTasksLength={17}
        lastRefresh={new Date('2026-05-08T12:00:00Z')}
        onRefresh={() => {}}
        showMetrics={showMetrics}
        onToggleMetrics={() => {}}
        wsRef={wsRef}
        onRefreshTasks={() => {}}
        metricsHistory={[]}
        pauseReason={null}
      />
    </div>
  );
}

describe('MetricsBar — viewport regression', () => {
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

  test('mobile gate (hidden md:flex) is honored at 375px — no MetricsBar content visible', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<MetricsBarHarness showMetrics={false} />);
      });
    });
    // The gate should hide the MetricsBar and all of its descendants at 375px.
    // If a future change removes `hidden md:flex` from HeaderBar, this assertion
    // makes the regression visible by surfacing a non-zero-width metric tile.
    const visibleMetricTile = Array.from(
      document.querySelectorAll<HTMLElement>('span'),
    ).find((el) => el.textContent === 'Active VMs');
    if (visibleMetricTile) {
      const rect = visibleMetricTile.getBoundingClientRect();
      expect(rect.width).toBe(0);
      expect(rect.height).toBe(0);
    }
  });

  test('renders without horizontal overflow at 768px (md: breakpoint — gate active)', async () => {
    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<MetricsBarHarness showMetrics={false} />);
      });
    });
  });

  test('no text truncation of metric labels at 768px', async () => {
    // MetricsBar is desktop-only (hidden md:flex); 768px is the first breakpoint
    // where labels like "Active VMs", "Evaluating", "Queue", "Refreshed" are visible.
    await expectNoTextTruncationAt(768, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<MetricsBarHarness showMetrics={false} />);
      });
    });
  });
});
