import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { UILoadTimingPanel } from './UILoadTimingPanel';
import { expectNoOverflowAt } from '../test/viewport';

const MOCK_TIMING_DATA = {
  surfaces: {
    chat: [
      { ts: '2026-07-09T07:31:00Z', page_ready_ms: 823, sample_type: 'first_commit' },
      { ts: '2026-07-09T07:31:05Z', page_ready_ms: 345, sample_type: 'chat_data_ready' },
      { ts: '2026-07-09T08:00:00Z', page_ready_ms: 1842, sample_type: 'first_commit' },
      { ts: '2026-07-09T08:00:05Z', page_ready_ms: 892, sample_type: 'chat_data_ready' },
      { ts: '2026-07-09T09:00:00Z', page_ready_ms: 654, sample_type: 'first_commit' },
      { ts: '2026-07-09T09:00:05Z', page_ready_ms: 210, sample_type: 'chat_data_ready' },
    ],
    tasks: [
      { ts: '2026-07-09T07:30:00Z', page_ready_ms: 451, sample_type: 'first_commit' },
      { ts: '2026-07-09T08:30:00Z', page_ready_ms: 1021, sample_type: 'first_commit' },
    ],
    'project-detail': [
      { ts: '2026-07-09T07:45:00Z', page_ready_ms: 310, sample_type: 'first_commit' },
      { ts: '2026-07-09T07:45:03Z', page_ready_ms: 195, sample_type: 'chat_data_ready' },
    ],
  },
};

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    apiFetch: vi.fn(async () =>
      new Response(JSON.stringify(MOCK_TIMING_DATA), { status: 200 }),
    ),
    authGet: vi.fn(async () => new Response('{}', { status: 200 })),
  });
});

describe('UILoadTimingPanel viewport', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) act(() => { r.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('renders without overflow at 375px (mobile)', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<UILoadTimingPanel />); });
      // Wait for usePoller leading call to resolve with mocked data
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    });
  });

  test('renders without overflow at 1280px (desktop wide)', async () => {
    await expectNoOverflowAt(1280, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<UILoadTimingPanel />); });
      // Wait for usePoller leading call to resolve with mocked data
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    });
  });
});
