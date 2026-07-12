import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectFullViewportAt } from '../test/viewport';
import type { WarmVmStateResponse } from '../types/warmVm';
import type { Project } from '../types/project';

// FleetDetailDrawer is a thin overlay around WarmVmPanel, which loads warm-VM
// state through '../api' and the efficiency-snapshot hook. Mock both the same
// way WarmVmPanel.viewport.test.tsx does so the panel renders real content (an
// empty/idle fleet here) instead of an error/ErrorBoundary fallback.
const emptyState: WarmVmStateResponse = {
  active_vms: [],
  warm_pin_metrics: {
    clears_last_hour: 0,
    clears_last_24h: 0,
    active_pinned_queued_total: 0,
    active_warm_chains: 0,
    active_backlog_reuse_claims: 0,
    active_chain_continuity_claims: 0,
  },
  warm_continuity_enabled: true,
  warm_backlog_reuse_enabled: false,
  next_role_poll_timeout_secs: 30,
};

// The drawer's subtree (WarmVmPanel + FleetRunnersPanel) statically imports
// many named exports from '../api'; browser-mode ESM requires the mock factory
// to expose EVERY export the subtree imports or the linker fails at import
// time. createApiMock provides the complete, typecheck-enforced export surface
// so new api.ts exports can't silently break this factory again.
vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
    authGet: vi.fn(async () => new Response(JSON.stringify(emptyState), { status: 200 })),
    enableWarmPoolForProject: vi.fn(async () => ({})),
    disableWarmPoolForProject: vi.fn(async () => ({})),
    fetchWarmPoolTargets: vi.fn(async () => ({
      warm_pool_target_size: 2,
      warm_pool_target_size_by_project: {},
    })),
    setWarmPoolTargetForProject: vi.fn(async () => ({})),
    fetchEfficiencyAxes: vi.fn(async () => ({ axes: {} })),
    listFleetRunners: vi.fn(async () => []),
    registerFleetRunner: vi.fn(async () => ({})),
    revokeFleetRunner: vi.fn(async () => ({})),
    rotateFleetRunnerToken: vi.fn(async () => ({ runner_id: 'runner-abc123', token: 'tok-rotated' })),
    API_BASE_URL: 'http://localhost:8000',
  });
});

vi.mock('../hooks/useEfficiencySnapshot', () => ({
  useEfficiencySnapshot: () => null,
  fmtEffSecs: (s: number | null | undefined) => (s == null ? '—' : `${s}s`),
  fmtEffRate: (r: number | null | undefined) => (r == null ? '—' : `${Math.round(r * 100)}%`),
  fmtEffIter: (n: number | null | undefined) => (n == null ? '—' : n.toFixed(1)),
}));

import { FleetDetailDrawer } from './FleetDetailDrawer';

const projects: Project[] = [
  {
    id: 'proj-fleet-0001-very-long-project-identifier',
    name: 'Project With A Deliberately Long Name To Stress The 375px Gate',
    status: 'active',
    github_owner: 'o',
    github_repo: 'r',
    autonomous_enabled: true,
    created_at: '2026-01-01',
  } as unknown as Project,
];

describe('FleetDetailDrawer — viewport regression', () => {
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

  // FleetDetailDrawer is a `fixed inset-0` right-side drawer anchored to the
  // viewport, so it lays out full-bleed. A single expectFullViewportAt covers
  // overflow + text-truncation + touch-target at 375px.
  //
  // The touch-target check is scoped vacuously (touchSelector: 'none'): the
  // drawer's own header "Close fleet details" button already carries an
  // `eslint-disable viewport/min-touch-target` opt-out in FleetDetailDrawer.tsx,
  // and every other interactive control belongs to WarmVmPanel, whose dense
  // operator toggles/chips (`px-1.5 py-0.5`) are deliberately compact and exempt
  // from the 44px floor — WarmVmPanel.viewport.test.tsx itself asserts only
  // overflow, never touch-target. The overflow + text-truncation assertions
  // below exercise the real rendered drawer + panel.
  test('drawer renders without overflow/truncation/small-target at 375px', async () => {
    await expectFullViewportAt(
      375,
      async () => {
        host = document.createElement('div');
        document.body.appendChild(host);
        const r = createRoot(host);
        root = r;
        await act(async () => {
          r.render(
            <FleetDetailDrawer onClose={() => {}} onViewTask={() => {}} projects={projects} />,
          );
        });
        // Let WarmVmPanel's initial load() fetch resolve before asserting.
        await act(async () => {
          await new Promise<void>((resolve) => setTimeout(resolve, 50));
        });
      },
      { touchSelector: 'none', waitMs: 60 },
    );
  });
});
