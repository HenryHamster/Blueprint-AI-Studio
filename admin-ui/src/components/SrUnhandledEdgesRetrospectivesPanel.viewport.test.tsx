import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { screen, fireEvent } from '@testing-library/react';
import { SrUnhandledEdgesRetrospectivesPanel } from './SrUnhandledEdgesRetrospectivesPanel';
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
    task_id: 'task-edge-1',
    title: 'feat: add retry-on-conflict logic for concurrent task scheduling',
    task_type: 'create',
    scope: 'medium',
    created_at: new Date(Date.now() - 2 * 86400_000).toISOString(),
    unhandled_edges: [
      { case: 'concurrent writes race', handled: false, blind_spot: 'no lock around state mutation', origin: 'self-review' },
      { case: 'partial retry on network timeout', handled: false, blind_spot: null, origin: 'reviewer' },
    ],
    pr_url: 'https://github.com/example/repo/pull/77',
  },
  {
    task_id: 'task-edge-2',
    title: 'fix: handle empty payload in queue dispatcher without crashing',
    task_type: 'fix',
    scope: 'small',
    created_at: new Date(Date.now() - 4 * 86400_000).toISOString(),
    unhandled_edges: [
      { case: 'empty list still triggers post-process hook', handled: false, blind_spot: 'hook fires unconditionally', origin: 'self-review' },
    ],
    pr_url: null,
  },
];

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response(JSON.stringify({}), { status: 200 })),
  fetchSrUnhandledEdgesRetrospectives: vi.fn(async () => ({
    items: MOCK_ITEMS,
    total: 2,
    task_types: ['create', 'fix'],
  })),
  API_BASE_URL: 'http://localhost:8000',
  authGet: vi.fn(async () => new Response(JSON.stringify({}), { status: 200 })),
}));

describe('SrUnhandledEdgesRetrospectivesPanel — viewport regression', () => {
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
          <SrUnhandledEdgesRetrospectivesPanel
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

  test('shows all-clear empty state when no unhandled edges and no filter active', async () => {
    const { fetchSrUnhandledEdgesRetrospectives } = await import('../api');
    (fetchSrUnhandledEdgesRetrospectives as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ items: [], total: 0, task_types: [] });

    host = document.createElement('div');
    host.style.width = '375px';
    host.style.height = '700px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <SrUnhandledEdgesRetrospectivesPanel
          selectedProjectId="proj-1"
          onSelectTask={() => {}}
        />,
      );
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    const emptyEl = screen.getByTestId('sr-unhandled-edges-empty');
    expect(emptyEl).toBeTruthy();
    expect(emptyEl.textContent).toContain('No unhandled edges');
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
        <SrUnhandledEdgesRetrospectivesPanel
          selectedProjectId={null}
          onSelectTask={() => {}}
        />,
      );
    });
    const panel = host.querySelector('[data-testid="sr-unhandled-edges-retrospectives-panel"]');
    if (panel) throw new Error('Expected panel to not render when no project selected');
  });

  test('expanded row reveals unhandled edge case text and blind_spot', async () => {
    host = document.createElement('div');
    host.style.width = '800px';
    host.style.height = '900px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <SrUnhandledEdgesRetrospectivesPanel
          selectedProjectId="proj-1"
          onSelectTask={() => {}}
        />,
      );
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    const expandBtns = screen.getAllByRole('button', { name: /^expand /i });
    await act(async () => {
      fireEvent.click(expandBtns[0]);
    });

    expect(screen.getByText('concurrent writes race')).toBeTruthy();
    expect(screen.getByText('no lock around state mutation')).toBeTruthy();
  });

  test('refresh button visible when stale and single-page result set (totalPages=1)', async () => {
    const { fetchSrUnhandledEdgesRetrospectives } = await import('../api');
    const spy = fetchSrUnhandledEdgesRetrospectives as ReturnType<typeof vi.fn>;
    spy.mockClear();
    // Return 2 items — well under PAGE_SIZE=20, so totalPages=1 and pagination is hidden
    spy.mockResolvedValue({ items: MOCK_ITEMS, total: 2, task_types: ['create', 'fix'] });

    // Pin Date.now() to a fixed value so panelMountTimeRef captures it
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(1_000_000);

    host = document.createElement('div');
    host.style.width = '800px';
    host.style.height = '900px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <SrUnhandledEdgesRetrospectivesPanel
          selectedProjectId="proj-1"
          onSelectTask={() => {}}
        />,
      );
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    // Refresh button must NOT be present when fresh
    expect(screen.queryByRole('button', { name: /refresh pagination data/i })).toBeNull();

    // Advance time past the 5-minute stale threshold
    nowSpy.mockReturnValue(1_000_000 + 310_000);

    // Re-render with same props to trigger isStale recomputation
    await act(async () => {
      r.render(
        <SrUnhandledEdgesRetrospectivesPanel
          selectedProjectId="proj-1"
          onSelectTask={() => {}}
        />,
      );
    });

    // Refresh button must now be visible even though totalPages=1 (no pagination shown)
    expect(screen.getByRole('button', { name: /refresh pagination data/i })).toBeTruthy();

    nowSpy.mockRestore();
  });

  test('initial fetch passes page=0 to the API matching 0-indexed backend contract', async () => {
    const { fetchSrUnhandledEdgesRetrospectives } = await import('../api');
    const spy = fetchSrUnhandledEdgesRetrospectives as ReturnType<typeof vi.fn>;
    spy.mockClear();

    host = document.createElement('div');
    host.style.width = '800px';
    host.style.height = '900px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <SrUnhandledEdgesRetrospectivesPanel
          selectedProjectId="proj-1"
          onSelectTask={() => {}}
        />,
      );
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    expect(spy).toHaveBeenCalledWith('proj-1', expect.objectContaining({ page: 0 }));
  });

  test('filtered-empty state shows filter-aware message, not all-clear', async () => {
    const { fetchSrUnhandledEdgesRetrospectives } = await import('../api');
    (fetchSrUnhandledEdgesRetrospectives as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ items: MOCK_ITEMS, total: 2, task_types: ['create', 'fix'] })
      .mockResolvedValueOnce({ items: [], total: 0, task_types: ['create', 'fix'] });

    host = document.createElement('div');
    host.style.width = '800px';
    host.style.height = '900px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <SrUnhandledEdgesRetrospectivesPanel
          selectedProjectId="proj-1"
          onSelectTask={() => {}}
        />,
      );
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    const taskTypeSelect = screen.getByRole('combobox', { name: /filter by task type/i });
    await act(async () => {
      fireEvent.change(taskTypeSelect, { target: { value: 'create' } });
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    const emptyEl = screen.getByTestId('sr-unhandled-edges-empty');
    expect(emptyEl).toBeTruthy();
    expect(emptyEl.textContent).not.toContain('No unhandled edges in the last');
    expect(emptyEl.textContent).toContain('No unhandled edges for type');
  });
});
