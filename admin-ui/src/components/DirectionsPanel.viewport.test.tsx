import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { DirectionsPanel } from './DirectionsPanel';
import type { Direction } from './DirectionsPanel';
import { expectNoOverflowAt, expectMinTouchTargetAt } from '../test/viewport';

// Partial mock — preserve every other export from `../api` so transitive
// imports (TaskCard's `authGet`, etc.) resolve. Browser-mode vitest
// validates ESM exports statically at import time, so a static factory
// that omits any transitively-needed name fails with `does not provide an
// export named ...`. See feedback memory `vi.mock partial pattern`.
vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>();
  return {
    ...actual,
    apiFetch: vi.fn(async (url: string) => {
      if (url.includes('/directions') && !url.includes('/directions/')) {
        return new Response(
          JSON.stringify({
            directions: [
              {
                id: 'dir-high',
                text: 'Improve real-time notification system for collaborative features',
                priority: 'high',
                status: 'active',
                category: 'product',
                added_at: '2026-04-01T00:00:00Z',
                completion_criteria: 'Users receive notifications within 100ms',
              },
              {
                id: 'dir-medium',
                text: 'Refactor authentication middleware to reduce token validation overhead',
                priority: 'medium',
                status: 'active',
                category: 'structural',
                added_at: '2026-04-10T00:00:00Z',
                completion_criteria: 'Auth latency reduced to <50ms per request',
              },
              {
                id: 'dir-low',
                text: 'Add comprehensive logging for debugging and monitoring',
                priority: 'low',
                status: 'paused',
                category: 'mixed',
                added_at: '2026-04-15T00:00:00Z',
                completion_criteria: 'All critical paths logged at INFO level',
              },
            ] as Direction[],
            autonomous_enabled: true,
            design_doc: {
              'capabilities/api': { status: 'mature' },
              'capabilities/auth': { status: 'functional' },
              _meta: { last_updated: '2026-05-05T10:00:00Z' },
            },
            problem_model: {
              intent: { purpose: 'Improve system reliability' },
              capabilities: [
                { name: 'api', status: 'working' },
                { name: 'auth', status: 'working' },
                { name: 'notifications', status: 'partial' },
              ],
              gaps: [
                { name: 'real-time sync', description: 'Needs WebSocket support' },
              ],
              last_updated: '2026-05-05T11:00:00Z',
            },
            running_update: null,
            running_update_acknowledged_at: null,
          }),
          { status: 200 },
        );
      }
      // All other endpoints return empty/ok
      return new Response('{}', { status: 200 });
    }),
  };
});

describe('DirectionsPanel — viewport regression', () => {
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

  test('directions tab renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <DirectionsPanel
            onClose={() => {}}
            addToast={() => {}}
            selectedProjectId="proj-test"
            projects={[{
              id: 'proj-test',
              name: 'Test Project',
              status: 'active',
              github_owner: 'owner',
              github_repo: 'repo',
              autonomous_enabled: true,
              directions: [],
              created_at: '2026-01-01T00:00:00Z',
            }]}
          />,
        );
      });
      // Allow async state updates and fetches to settle
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    });
  });

  test('problem-model tab renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <DirectionsPanel
            onClose={() => {}}
            addToast={() => {}}
            selectedProjectId="proj-test"
            projects={[{
              id: 'proj-test',
              name: 'Test Project',
              status: 'active',
              github_owner: 'owner',
              github_repo: 'repo',
              autonomous_enabled: true,
              directions: [],
              created_at: '2026-01-01T00:00:00Z',
            }]}
          />,
        );
      });
      // Allow initial render and data fetch to settle
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
      // Click the problem-model tab to switch to findings
      const problemModelTab = document.querySelector('button[class*="text-sm font-medium"]');
      if (problemModelTab) {
        const buttons = Array.from(document.querySelectorAll('button')).filter((b) =>
          b.textContent?.includes('Problem Model'),
        );
        if (buttons[0]) {
          await act(async () => {
            buttons[0].click();
          });
        }
      }
      // Let layout settle after tab switch
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    });
  });

  test('interactive controls meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <DirectionsPanel
            onClose={() => {}}
            addToast={() => {}}
            selectedProjectId="proj-test"
            projects={[{
              id: 'proj-test',
              name: 'Test Project',
              status: 'active',
              github_owner: 'owner',
              github_repo: 'repo',
              autonomous_enabled: true,
              directions: [],
              created_at: '2026-01-01T00:00:00Z',
            }]}
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    }, { selector: 'button, a[href]' });
  });
});
