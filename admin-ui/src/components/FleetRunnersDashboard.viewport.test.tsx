import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectFullViewportAt } from '../test/viewport';
import type { FleetHealthSummaryResponse } from '../api';

const emptyFleetSummary: FleetHealthSummaryResponse = {
  total: 0,
  connected: 0,
  degraded: 0,
  stale: 0,
  offline: 0,
  low_disk: 0,
  low_memory: 0,
  per_project: [],
  project_count: 0,
  generated_at: '2026-07-22T00:00:00Z',
};

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    fetchFleetHealthSummary: vi.fn(async () => emptyFleetSummary),
  });
});

import { FleetRunnersDashboard } from './FleetRunnersDashboard';

describe('FleetRunnersDashboard — viewport regression', () => {
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

  test('renders without overflow/truncation at 375px', async () => {
    await expectFullViewportAt(
      375,
      async () => {
        host = document.createElement('div');
        document.body.appendChild(host);
        const r = createRoot(host);
        root = r;
        await act(async () => {
          r.render(<FleetRunnersDashboard />);
        });
        await act(async () => {
          await new Promise<void>((resolve) => setTimeout(resolve, 50));
        });
      },
      { touchSelector: 'none', waitMs: 60 },
    );
  });
});
