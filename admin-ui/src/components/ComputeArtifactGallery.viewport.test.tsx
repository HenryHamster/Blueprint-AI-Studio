import { afterEach, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectFullViewportAt } from '../test/viewport';
import type { ComputeJob } from '../api';
import { ComputeArtifactGallery } from './ComputeArtifactGallery';

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    resolveArtifactDownloadUrl: vi.fn((_projectId: string, storageUri: string) =>
      Promise.resolve(`https://fresh.example.com/signed?ref=${encodeURIComponent(storageUri)}`),
    ),
  });
});

function makeJob(overrides: Partial<ComputeJob> = {}): ComputeJob {
  return {
    id: 'job-1',
    project_id: 'proj-1',
    task_id: 'task-1',
    backend: 'runner-subprocess',
    external_compute_id: null,
    machine_type: null,
    status: 'SUCCEEDED',
    terminal_reason: null,
    outcome: 'SUCCESS',
    spend_cap_usd: 5,
    wall_clock_deadline: null,
    created_at: '2026-07-11T10:00:00Z',
    updated_at: '2026-07-11T10:30:00Z',
    started_at: '2026-07-11T10:00:10Z',
    completed_at: '2026-07-11T10:29:50Z',
    last_polled_at: '2026-07-11T10:29:50Z',
    accrued_cost_usd: 0,
    final_cost_usd: 0,
    result_reference: null,
    accelerator_type: null,
    accelerator_count: null,
    ...overrides,
  } as ComputeJob;
}

const viewableArtifacts = [
  { storage_uri: 'gs://bucket/plot.png', filename: 'reward-plot.png', content_type: 'image/png', size_bytes: 2048, kind: 'image' as const },
  { storage_uri: 'gs://bucket/rollout.mp4', filename: 'rollout.mp4', content_type: 'video/mp4', size_bytes: 5_000_000, kind: 'video' as const },
];

describe('ComputeArtifactGallery — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) { act(() => r.unmount()); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders the collapsed gallery toggle at 375px without overflow', async () => {
    await expectFullViewportAt(
      375,
      async () => {
        host = document.createElement('div');
        host.style.padding = '16px';
        document.body.appendChild(host);
        const r = createRoot(host);
        root = r;
        await act(async () => {
          r.render(<ComputeArtifactGallery projectId="proj-1" job={makeJob({ viewable_artifacts: viewableArtifacts })} />);
        });
        await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 30)); });
        const toggle = host.querySelector('[data-testid="drawer-artifact-gallery-toggle"]');
        expect(toggle).not.toBeNull();
        expect((host.textContent ?? '')).toContain('Show previews (2)');
      },
      { waitMs: 40 },
    );
  });

  test('renders the expanded gallery previews at 900px without overflow', async () => {
    await expectFullViewportAt(
      900,
      async () => {
        host = document.createElement('div');
        host.style.padding = '16px';
        document.body.appendChild(host);
        const r = createRoot(host);
        root = r;
        await act(async () => {
          r.render(<ComputeArtifactGallery projectId="proj-1" job={makeJob({ viewable_artifacts: viewableArtifacts })} />);
        });
        const toggle = host.querySelector<HTMLButtonElement>('[data-testid="drawer-artifact-gallery-toggle"]');
        expect(toggle).not.toBeNull();
        await act(async () => { toggle!.click(); });
        await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 30)); });
        // Both viewable artifacts render (image thumbnail + video) once resolved.
        expect(host.querySelectorAll('[data-testid="drawer-viewable-artifact"]').length).toBe(2);
      },
      { waitMs: 40 },
    );
  });
});
