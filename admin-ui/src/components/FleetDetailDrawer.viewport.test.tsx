import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectFullViewportAt } from '../test/viewport';
import type { Project } from '../types/project';

// The drawer's FleetRunnersPanel subtree statically imports
// many named exports from '../api'; browser-mode ESM requires the mock factory
// to expose EVERY export the subtree imports or the linker fails at import
// time. createApiMock provides the complete, typecheck-enforced export surface
// so new api.ts exports can't silently break this factory again.
vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
    listFleetRunners: vi.fn(async () => []),
    registerFleetRunner: vi.fn(async () => ({})),
    revokeFleetRunner: vi.fn(async () => ({})),
    rotateFleetRunnerToken: vi.fn(async () => ({ runner_id: 'runner-abc123', token: 'tok-rotated' })),
    API_BASE_URL: 'http://localhost:8000',
  });
});

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
    my_role: 'viewer',
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
  // and the runner panel has its own touch-target coverage. The overflow and
  // text-truncation assertions below exercise the real read-only drawer path.
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
            <FleetDetailDrawer
              onClose={() => {}}
              onViewTask={() => {}}
              projects={projects}
              selectedProjectId={projects[0].id}
            />,
          );
        });
        // Let FleetRunnersPanel's initial load resolve before asserting.
        await act(async () => {
          await new Promise<void>((resolve) => setTimeout(resolve, 50));
        });
      },
      { touchSelector: 'none', waitMs: 60 },
    );
  });
});
