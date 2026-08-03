import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { FleetRunnersPanel } from './FleetRunnersPanel';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';
import type { FleetRunner } from '../api';
import { listFleetRunners } from '../api';

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
    // Project allow-list so the Projects section renders in the 1280px acceptance
    // capture: the enrollment/default project plus one additional allowed project.
    listRunnerProjects: vi.fn(async (_p: string, runnerId: string) => ({
      runner_id: runnerId,
      enrollment_project_id: 'proj-canary',
      projects: [
        { project_id: 'proj-canary', is_enrollment: true },
        { project_id: 'proj-extra', is_enrollment: false },
      ],
    })),
    listOwnedProjects: vi.fn(async () => [
      { id: 'proj-canary', name: 'canary-project' },
      { id: 'proj-extra', name: 'extra-project' },
      { id: 'proj-third', name: 'third-project' },
    ]),
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
      r.render(<FleetRunnersPanel projectId="proj-canary" readOnly={false} />);
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

// ── Enroll-dialog viewport walkthrough at 1280px ─────────────────────────────
// Each test captures one step of the enroll flow as a distinct PNG in capture
// mode (maybeCapture inside the helpers uses the test name as the slug).

describe('FleetRunnersPanel — enroll-dialog viewport walkthrough', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) { act(() => { r.unmount(); }); root = null; }
    if (host) { host.remove(); host = null; }
    vi.clearAllMocks();
  });

  async function mountEnroll(width: number) {
    host = document.createElement('div');
    host.style.width = `${width}px`;
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<FleetRunnersPanel projectId="proj-canary" readOnly={false} />);
    });
    await act(async () => {});
    // Open the enroll dialog.
    const enrollBtn = [...host.querySelectorAll('button')].find(
      b => b.textContent?.includes('Enroll runner'),
    );
    if (!enrollBtn) throw new Error('Enroll runner button not found');
    await act(async () => { (enrollBtn as HTMLButtonElement).click(); });
    await act(async () => {});
  }

  test('enroll dialog — token step with prerequisites renders without overflow at 1280px', async () => {
    await expectNoOverflowAt(1280, async () => { await mountEnroll(1280); });
  });

  test('enroll dialog — waiting state renders without overflow at 1280px', async () => {
    // After enrolling, the dialog should be in "Waiting for your runner to connect"
    // state by default (runner-new has not heartbeated yet).
    await expectNoOverflowAt(1280, async () => {
      await mountEnroll(1280);
      // Verify the waiting state is actually shown so the screenshot is meaningful.
      const statusEl = host?.querySelector('[data-testid="runner-connection-status"]');
      if (!statusEl?.textContent?.includes('Waiting')) {
        throw new Error(`Expected waiting state but got: ${statusEl?.textContent}`);
      }
    });
  });

  test('enroll dialog — connected state renders without overflow at 1280px', async () => {
    const connectedNew: FleetRunner = {
      id: 'runner-new',
      hostname: 'viewport-host',
      version: null,
      status: 'active',
      capabilities: { task_types: 'all', has_gpu: false, max_concurrent: 1 },
      health: {
        device_status: 'connected',
        device_status_reason: null,
        is_live: true,
        seconds_since_last_seen: 2,
        active_task_count: 0,
        active_compute_job_count: 0,
        active_work_count: 0,
        max_concurrent: 1,
        available_slots: 1,
        stale_reason: null,
        agent_available: true,
        liveness_window_seconds: 300,
      },
      last_seen_at: new Date().toISOString(),
      created_at: '2026-07-14T00:00:00Z',
    };
    await expectNoOverflowAt(1280, async () => {
      await mountEnroll(1280);
      // Simulate the runner's first heartbeat via the list refresh button.
      vi.mocked(listFleetRunners).mockResolvedValue([...SAMPLE_RUNNERS, connectedNew]);
      const refreshBtn = host?.querySelector('button[aria-label="Refresh runners"]');
      if (refreshBtn) {
        await act(async () => { (refreshBtn as HTMLButtonElement).click(); });
        await act(async () => {});
      }
    });
  });

  test('enroll dialog — timeout fallback renders without overflow at 1280px', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(listFleetRunners).mockResolvedValue(SAMPLE_RUNNERS);
      await expectNoOverflowAt(1280, async () => {
        await mountEnroll(1280);
        await act(async () => { await vi.advanceTimersByTimeAsync(95_000); });
        // Verify "Not connected yet" is shown so the screenshot is meaningful.
        const statusEl = host?.querySelector('[data-testid="runner-connection-status"]');
        if (!statusEl?.textContent?.includes('Not connected yet')) {
          throw new Error(`Expected timeout state but got: ${statusEl?.textContent}`);
        }
      });
    } finally {
      vi.useRealTimers();
    }
  });
});
