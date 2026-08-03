import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act, type ComponentProps } from 'react';
import { AlertsBanner as AlertsBannerComponent } from './AlertsBanner';
import type { Alert } from '../types/alert';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';

type AlertsBannerTestProps = Omit<ComponentProps<typeof AlertsBannerComponent>, 'canMutate'> & {
  canMutate?: boolean;
};

function AlertsBanner(props: AlertsBannerTestProps) {
  return <AlertsBannerComponent canMutate={true} {...props} />;
}

// AlertsBanner imports `apiFetch` from '../api', which transitively pulls in
// firebase. We don't pass projectId in this test (so the fetch path never
// fires), but stubbing the module keeps the canary independent of real auth
// at module-load time.
vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
}));

const sampleAlerts: Alert[] = [
  {
    id: 'alert-1',
    project_id: 'project-canary',
    severity: 'WARNING',
    title: 'Self-review catch-rate dropped',
    message: 'correctness category fell from 0.62 -> 0.41 over the last 3 cycles',
    task_id: 'task-1234',
    created_at: '2026-05-06T00:00:00Z',
    dismissed_at: null,
    acknowledged_at: null,
  },
  {
    id: 'alert-2',
    project_id: 'project-canary',
    severity: 'ERROR',
    title: 'Pipeline regression detected',
    message: 'agent_timeout_rate breached threshold on three consecutive cycles',
    task_id: null,
    created_at: '2026-05-06T00:01:00Z',
    dismissed_at: null,
    acknowledged_at: null,
  },
];

describe('AlertsBanner — viewport regression', () => {
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

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      // Match the app shell padding (DashboardHome wraps in `p-6`); narrower
      // than the viewport so we don't accidentally test full-bleed layouts.
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <AlertsBanner
            alerts={sampleAlerts}
            onDismiss={() => {}}
            onDismissAll={() => {}}
            onViewTask={() => {}}
          />,
        );
      });
    });
  });

  test('renders without horizontal overflow at 768px (tablet md: breakpoint)', async () => {
    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <AlertsBanner
            alerts={sampleAlerts}
            onDismiss={() => {}}
            onDismissAll={() => {}}
            onViewTask={() => {}}
          />,
        );
      });
    });
  });

  test('action buttons meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <AlertsBanner
            alerts={sampleAlerts}
            onDismiss={() => {}}
            onDismissAll={() => {}}
            onViewTask={() => {}}
            projectId="project-canary"
          />,
        );
      });
    });
  });

  test('alert titles and messages are not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <AlertsBanner
            alerts={sampleAlerts}
            onDismiss={() => {}}
            onDismissAll={() => {}}
            onViewTask={() => {}}
          />,
        );
      });
    });
  });
});
