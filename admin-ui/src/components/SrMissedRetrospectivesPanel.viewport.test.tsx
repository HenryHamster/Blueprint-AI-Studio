import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { screen } from '@testing-library/react';
import { SrMissedRetrospectivesPanel } from './SrMissedRetrospectivesPanel';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  signOut: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
}));

const MOCK_ITEMS = [
  {
    task_id: 'task-sr-1',
    title: 'feat: add OAuth2 PKCE flow with refresh token rotation and session expiry',
    task_type: 'create',
    scope: 'medium',
    created_at: new Date(Date.now() - 2 * 86400_000).toISOString(),
    missed_categories: ['correctness', 'error-handling'],
    caught_categories: ['security', 'type-safety'],
    pr_url: 'https://github.com/example/repo/pull/42',
  },
  {
    task_id: 'task-sr-2',
    title: 'fix: resolve connection pool exhaustion under high concurrent load scenarios',
    task_type: 'fix',
    scope: 'small',
    created_at: new Date(Date.now() - 5 * 86400_000).toISOString(),
    missed_categories: ['performance'],
    caught_categories: [],
    pr_url: null,
  },
];

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response(JSON.stringify({}), { status: 200 })),
  fetchSrMissedRetrospectives: vi.fn(async () => ({
    items: MOCK_ITEMS,
    total: 2,
  })),
  API_BASE_URL: 'http://localhost:8000',
  authGet: vi.fn(async () => new Response(JSON.stringify({}), { status: 200 })),
}));

describe('SrMissedRetrospectivesPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) { act(() => { r.unmount(); }); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.width = '375px';
      host.style.height = '700px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <SrMissedRetrospectivesPanel
            selectedProjectId="proj-1"
            onSelectTask={() => {}}
          />,
        );
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
      });
    });
  });

  test('shows empty state when no SR misses', async () => {
    const { fetchSrMissedRetrospectives } = await import('../api');
    (fetchSrMissedRetrospectives as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ items: [], total: 0 });

    host = document.createElement('div');
    host.style.width = '375px';
    host.style.height = '700px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <SrMissedRetrospectivesPanel
          selectedProjectId="proj-1"
          onSelectTask={() => {}}
        />,
      );
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    const emptyEl = screen.getByTestId('sr-missed-empty');
    if (!emptyEl) throw new Error('Expected empty state element to be present');
  });

  test('returns null when no project selected', async () => {
    host = document.createElement('div');
    host.style.width = '375px';
    host.style.height = '700px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <SrMissedRetrospectivesPanel
          selectedProjectId={null}
          onSelectTask={() => {}}
        />,
      );
    });
    const panel = host.querySelector('[data-testid="sr-missed-retrospectives-panel"]');
    if (panel) throw new Error('Expected panel to not render when no project selected');
  });
});
