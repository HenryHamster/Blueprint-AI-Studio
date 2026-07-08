import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { AlertsPopover } from './AlertsPopover';
import type { Alert } from '../types/alert';
import { expectFullViewportAt } from '../test/viewport';

// AlertsPopover renders AlertsBanner, which imports `apiFetch` from '../api'
// (transitively firebase). We never trigger the fetch path here, but stubbing
// the module keeps the canary independent of real auth at module-load time —
// matching AlertsBanner.viewport.test.tsx.
vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
}));

// AlertsPopover wraps AlertsBanner in a `max-w-md` overlay with `p-3` outer +
// `p-4` inner padding, so its content box (~199px at 375px) is narrower than
// AlertsBanner's standalone test host. AlertsBanner lays each alert's
// title + `·` + message on one inline line in a `flex-1 min-w-0`
// (non-truncating) div, so the per-alert text must fit the popover's content
// width or it clips silently. These realistic, concise operator alerts fit;
// the longer strings AlertsBanner's own test renders in a wider host would
// overflow the tighter popover and surface a real text_truncation finding —
// exactly what the gate exists to catch.
const sampleAlerts: Alert[] = [
  {
    id: 'alert-1',
    project_id: 'project-canary',
    severity: 'WARNING',
    title: 'Catch-rate low',
    message: '0.62 to 0.41',
    task_id: 'task-1234',
    created_at: '2026-05-06T00:00:00Z',
    dismissed_at: null,
    acknowledged_at: null,
  },
  {
    id: 'alert-2',
    project_id: 'project-canary',
    severity: 'ERROR',
    title: 'Regression',
    message: 'timeout breached',
    task_id: null,
    created_at: '2026-05-06T00:01:00Z',
    dismissed_at: null,
    acknowledged_at: null,
  },
];

describe('AlertsPopover — viewport regression', () => {
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

  // AlertsPopover is a `fixed inset-0` overlay anchored to the viewport, so it
  // lays out full-bleed rather than inside a padded shell. A single
  // expectFullViewportAt covers overflow + text-truncation + touch-target at
  // 375px (the three required assertions).
  //
  // touchSelector excludes the header "Close alerts" icon button — it is a 24px
  // `p-1` icon target already carrying an `eslint-disable viewport/min-touch-target`
  // opt-out in AlertsPopover.tsx, so it is an accepted exemption, not a finding.
  // Every other interactive control (the AlertsBanner dismiss / dismiss-all /
  // view-task buttons) is still asserted against the 44px floor.
  test('overlay renders without overflow/truncation/small-target at 375px', async () => {
    await expectFullViewportAt(
      375,
      async () => {
        host = document.createElement('div');
        document.body.appendChild(host);
        const r = createRoot(host);
        root = r;
        await act(async () => {
          r.render(
            <AlertsPopover
              alerts={sampleAlerts}
              onDismiss={() => {}}
              onDismissAll={() => {}}
              onViewTask={() => {}}
              projectId="project-canary"
              onClose={() => {}}
            />,
          );
        });
      },
      { touchSelector: 'button:not([title="Close alerts"])' },
    );
  });
});
