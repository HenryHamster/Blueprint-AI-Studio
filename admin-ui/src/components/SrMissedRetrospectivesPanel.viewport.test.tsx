import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { screen, fireEvent } from '@testing-library/react';
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
    sr_check_results: {
      correctness: { passed: false, issue: 'Off-by-one in pagination logic' },
      'error-handling': { passed: true },
    },
    reviewer_findings: [
      { category: 'correctness', severity: 'HIGH', description: 'Pagination skips the last item due to incorrect boundary check' },
    ],
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
    sr_check_results: {
      performance: { passed: false, issue: 'Connection pool limit not adjusted for concurrent requests' },
    },
    reviewer_findings: [
      { category: 'performance', severity: 'CRITICAL', description: 'Connection pool exhausts under load — no backpressure mechanism' },
    ],
  },
];

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response(JSON.stringify({}), { status: 200 })),
  fetchSrMissedRetrospectives: vi.fn(async () => ({
    items: MOCK_ITEMS,
    total: 2,
    task_types: ['create', 'fix'],
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

  test('shows all-clear empty state when no SR misses and no filter active', async () => {
    const { fetchSrMissedRetrospectives } = await import('../api');
    (fetchSrMissedRetrospectives as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ items: [], total: 0, task_types: [] });

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
    expect(emptyEl.textContent).toContain('self-review is catching everything');
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

  test('expanded drill-down shows reviewer description and SR issue text per category', async () => {
    host = document.createElement('div');
    host.style.width = '800px';
    host.style.height = '900px';
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

    // Click the expand chevron on the first row (aria-label starts with "expand <title>")
    const expandBtns = screen.getAllByRole('button', { name: /^expand /i });
    await act(async () => {
      fireEvent.click(expandBtns[0]);
    });

    // Reviewer description text for the 'correctness' category must appear
    expect(screen.getByText('Pagination skips the last item due to incorrect boundary check')).toBeTruthy();
    // SR issue text for the 'correctness' category must also appear
    expect(screen.getByText('Off-by-one in pagination logic')).toBeTruthy();
  });

  test('filtered-empty state shows filter-aware message, not all-clear', async () => {
    const { fetchSrMissedRetrospectives } = await import('../api');
    // First fetch returns data with task_types; second (after filter) returns empty
    (fetchSrMissedRetrospectives as ReturnType<typeof vi.fn>)
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
        <SrMissedRetrospectivesPanel
          selectedProjectId="proj-1"
          onSelectTask={() => {}}
        />,
      );
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    // Apply a task-type filter — selector is populated via task_types from the server response
    const taskTypeSelect = screen.getByRole('combobox', { name: /filter by task type/i });
    await act(async () => {
      fireEvent.change(taskTypeSelect, { target: { value: 'create' } });
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    const emptyEl = screen.getByTestId('sr-missed-empty');
    expect(emptyEl).toBeTruthy();
    // Must NOT show the celebratory all-clear message when a filter is active
    expect(emptyEl.textContent).not.toContain('self-review is catching everything');
    // Must show a filter-aware "No misses" message instead
    expect(emptyEl.textContent).toContain('No misses');
    // Dropdown must stay visible — server always returns task_types regardless of filter,
    // so the selector should not disappear when filtered results are empty.
    const dropdownAfterFilter = screen.queryByRole('combobox', { name: /filter by task type/i });
    expect(dropdownAfterFilter).toBeTruthy();
  });

  test('syncs categoryFilter and resets page when activeCategory prop changes', async () => {
    const { fetchSrMissedRetrospectives } = await import('../api');
    const spy = fetchSrMissedRetrospectives as ReturnType<typeof vi.fn>;
    spy.mockClear();
    // First fetch: page 1 with items (so pagination appears), then re-fetch when activeCategory changes
    spy.mockResolvedValue({ items: MOCK_ITEMS, total: 40, task_types: ['create', 'fix'] });

    host = document.createElement('div');
    host.style.width = '800px';
    host.style.height = '900px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <SrMissedRetrospectivesPanel
          selectedProjectId="proj-1"
          onSelectTask={() => {}}
          activeCategory=""
        />,
      );
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    spy.mockClear();

    // Now update activeCategory to 'correctness' — should seed categoryFilter and trigger a re-fetch
    await act(async () => {
      r.render(
        <SrMissedRetrospectivesPanel
          selectedProjectId="proj-1"
          onSelectTask={() => {}}
          activeCategory="correctness"
        />,
      );
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    // The re-fetch should include the new category filter and reset to page 0
    expect(spy).toHaveBeenCalledWith('proj-1', expect.objectContaining({ category: 'correctness', page: 0 }));
  });

  test('refresh button visible when stale and single-page result set (totalPages=1)', async () => {
    const { fetchSrMissedRetrospectives } = await import('../api');
    const spy = fetchSrMissedRetrospectives as ReturnType<typeof vi.fn>;
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
        <SrMissedRetrospectivesPanel
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
        <SrMissedRetrospectivesPanel
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
    const { fetchSrMissedRetrospectives } = await import('../api');
    const spy = fetchSrMissedRetrospectives as ReturnType<typeof vi.fn>;
    spy.mockClear();

    host = document.createElement('div');
    host.style.width = '800px';
    host.style.height = '900px';
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

    // UI page 1 must map to API page 0 (backend is 0-indexed)
    expect(spy).toHaveBeenCalledWith('proj-1', expect.objectContaining({ page: 0 }));
  });
});
