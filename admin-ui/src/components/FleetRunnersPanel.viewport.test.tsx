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
    version: 'v22-3-gabcdef',
    status: 'active',
    // Runner reporting the full isolation-backend inventory + selected backends.
    capabilities: {
      task_types: 'all',
      has_gpu: true,
      max_concurrent: 2,
      isolation_backends: {
        bwrap_ok: true,
        userns_ok: true,
        docker_usable: true,
        podman_usable: true,
        apptainer_ok: true,
        enroot_ok: true,
        systemd_run_ok: true,
      },
      agent_backend: 'bwrap',
      compute_backend: 'docker',
    },
    // Live-config block so the per-job GPU count + task-type allowlist editors
    // render in the acceptance-evidence capture at 1280px.
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
  },
  {
    id: 'runner-def456',
    hostname: null,
    version: null,
    status: 'active',
    // Older runner with no isolation-backend inventory — Backends section hidden.
    capabilities: { task_types: ['compute_experiment'], has_gpu: false, max_concurrent: 1 },
    last_seen_at: null,
    created_at: '2026-06-10T00:00:00Z',
  },
];

// FleetRunnersPanel statically imports many named exports from '../api'
// (registration/rotation/revocation actions, capability + status patching,
// telemetry/stats detail loaders); browser-mode ESM requires the mock factory
// to expose EVERY export the module graph imports or the linker fails at
// import time with "does not provide an export named '<x>'". createApiMock
// provides the complete, typecheck-enforced export surface so new api.ts
// exports can't silently break this factory again. API_BASE_URL defaults to ''
// (mirrors the production VITE_API_URL || '' fallback) so the copy-paste
// registration snippet falls back to window.location.origin, as in
// same-origin dev.
vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    listFleetRunners: vi.fn(async () => SAMPLE_RUNNERS),
    registerFleetRunner: vi.fn(async () => ({ runner_id: 'runner-new', token: 'tok-secret' })),
    revokeFleetRunner: vi.fn(async () => {}),
    rotateFleetRunnerToken: vi.fn(async () => ({ runner_id: 'runner-abc123', token: 'tok-rotated' })),
  });
});

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

  // 1280px is the acceptance-evidence capture width: the isolation-backend
  // inventory row (backend pills + agent→/compute→ indicators) must not push the
  // card past the viewport at the operator's primary desktop width.
  test('renders backend inventory without horizontal overflow at 1280px', async () => {
    await expectNoOverflowAt(1280, async () => { await mount(1280); });
  });

  test('action buttons meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => { await mount(375); });
  });

  test('runner names are not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => { await mount(375); });
  });
});
