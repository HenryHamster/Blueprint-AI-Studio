import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { FleetRunnersPanel } from './FleetRunnersPanel';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';
import type { FleetRunner } from '../api';

const SAMPLE_RUNNERS: FleetRunner[] = [
  {
    id: 'runner-abc123',
    hostname: 'gpu-worker-01',
    version: '1.2.3',
    status: 'active',
    capabilities: { task_types: 'all', has_gpu: true, max_concurrent: 2 },
    last_seen_at: new Date(Date.now() - 30_000).toISOString(),
    created_at: '2026-06-01T00:00:00Z',
  },
  {
    id: 'runner-def456',
    hostname: null,
    version: null,
    status: 'active',
    capabilities: { task_types: ['compute_experiment'], has_gpu: false, max_concurrent: 1 },
    last_seen_at: null,
    created_at: '2026-06-10T00:00:00Z',
  },
];

vi.mock('../api', () => ({
  // FleetRunnersPanel now imports API_BASE_URL for the copy-paste runner
  // registration snippet; the mock must provide it or the browser-mode ESM
  // import fails with "does not provide an export named 'API_BASE_URL'".
  // Empty string mirrors the production default (VITE_API_URL || '') so the
  // component falls back to window.location.origin, as it does in same-origin dev.
  API_BASE_URL: '',
  listFleetRunners: vi.fn(async () => SAMPLE_RUNNERS),
  registerFleetRunner: vi.fn(async () => ({ runner_id: 'runner-new', token: 'tok-secret' })),
  revokeFleetRunner: vi.fn(async () => {}),
  // FleetRunnersPanel also statically imports rotateFleetRunnerToken for the
  // per-runner token-rotation action; browser-mode ESM requires the mock factory
  // to expose it or the linker fails at import time with "does not provide an
  // export named 'rotateFleetRunnerToken'".
  rotateFleetRunnerToken: vi.fn(async () => ({ runner_id: 'runner-abc123', token: 'tok-rotated' })),
}));

describe('FleetRunnersPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) { act(() => { r.unmount(); }); root = null; }
    if (host) { host.remove(); host = null; }
  });

  async function mount(width: number) {
    host = document.createElement('div');
    host.style.width = `${width}px`;
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<FleetRunnersPanel projectId="proj-canary" />);
    });
    // resolve the async fetch effect
    await act(async () => {});
  }

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => { await mount(375); });
  });

  test('renders without horizontal overflow at 768px', async () => {
    await expectNoOverflowAt(768, async () => { await mount(768); });
  });

  test('action buttons meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => { await mount(375); });
  });

  test('runner names are not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => { await mount(375); });
  });
});
