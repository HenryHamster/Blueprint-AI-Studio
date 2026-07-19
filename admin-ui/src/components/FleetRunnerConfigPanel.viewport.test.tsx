import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { FleetRunnerConfigPanel } from './FleetRunnerConfigPanel';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';
import type { FleetRunner } from '../api';

// A GPU-capable, non-strategic runner so every editable control renders (the
// per-job GPU count + GPU-index allowlist appear only when has_gpu is true, and
// concurrency/task-types are editable rather than read-only when the runner does
// not pin the strategic config).
const SAMPLE_RUNNER: FleetRunner = {
  id: 'runner-cfg-abc123',
  hostname: 'gpu-worker-config-01',
  version: 'v22-3-gabcdef',
  status: 'active',
  capabilities: {
    task_types: 'all',
    has_gpu: true,
    max_concurrent: 2,
  },
  config: {
    config_generation: 'gen-abc',
    applied_config_generation: 'gen-abc',
    config_pending: false,
    gpu_indices_allowed: 'all',
    gpus_per_job: 2,
    desired: { task_types: 'all' },
  },
  last_seen_at: new Date(Date.now() - 30_000).toISOString(),
  created_at: '2026-06-01T00:00:00Z',
};

// FleetRunnerConfigPanel statically imports updateRunnerCapabilities from
// '../api'; browser-mode ESM requires the mock factory to expose every export
// the module graph imports, so use the complete typed api mock.
vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    updateRunnerCapabilities: vi.fn(async () => {}),
  });
});

describe('FleetRunnerConfigPanel — viewport regression', () => {
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
      r.render(
        <FleetRunnerConfigPanel projectId="proj-canary" runner={SAMPLE_RUNNER} onSaved={() => {}} />,
      );
    });
    await act(async () => {});
  }

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => { await mount(375); });
  });

  test('renders without horizontal overflow at 768px', async () => {
    await expectNoOverflowAt(768, async () => { await mount(768); });
  });

  test('renders without horizontal overflow at 1280px', async () => {
    await expectNoOverflowAt(1280, async () => { await mount(1280); });
  });

  test('save control meets 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => { await mount(375); });
  });

  test('field labels are not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => { await mount(375); });
  });
});
