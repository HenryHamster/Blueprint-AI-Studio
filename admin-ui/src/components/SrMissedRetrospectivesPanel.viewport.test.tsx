import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
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

const MOCK_HEALTH_HEALTHY = {
  status: 'healthy' as const,
  data_sufficient: true,
  last_sufficient_computed_at: new Date(Date.now() - 90 * 60_000).toISOString(),
  last_sufficient_age_hours: 1.5,
  signal_last_computed_at: new Date(Date.now() - 90 * 60_000).toISOString(),
  signal_age_hours: 1.5,
  category_count: 4,
  categories_with_hit_rate: 4,
  overall_hit_rate: 0.75,
  previous_overall_hit_rate: 0.7,
  bucket_mode: 'cycle',
  prompt_version_changed: false,
  mixed_prompt_versions: false,
};

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response(JSON.stringify({}), { status: 200 })),
  fetchSrMissedRetrospectives: vi.fn(async () => ({
    items: MOCK_ITEMS,
    total: 2,
    task_types: ['create', 'fix'],
  })),
  fetchSrCatchRateHealth: vi.fn(async () => MOCK_HEALTH_HEALTHY),
  API_BASE_URL: 'http://localhost:8000',
  authGet: vi.fn(async () => new Response(JSON.stringify({}), { status: 200 })),
}));

describe('SrMissedRetrospectivesPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    window.history.replaceState({}, '', '/');
  });

  afterEach(() => {
    const r = root;
    if (r) { act(() => { r.unmount(); }); root = null; }
    if (host) { host.remove(); host = null; }
    window.history.replaceState({}, '', '/');
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

  test('caches catch-rate health by project and day window, but not pagination', async () => {
    const { fetchSrMissedRetrospectives, fetchSrCatchRateHealth } = await import('../api');
    const missSpy = fetchSrMissedRetrospectives as ReturnType<typeof vi.fn>;
    const healthSpy = fetchSrCatchRateHealth as ReturnType<typeof vi.fn>;
    missSpy.mockClear();
    healthSpy.mockClear();
    missSpy.mockResolvedValue({ items: MOCK_ITEMS, total: 40, task_types: ['create', 'fix'] });
    healthSpy
      .mockResolvedValueOnce(MOCK_HEALTH_HEALTHY)
      .mockResolvedValueOnce({
        ...MOCK_HEALTH_HEALTHY,
        status: 'stale',
        last_sufficient_age_hours: 30,
        signal_age_hours: 2,
      });

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

    expect(screen.getByTestId('sr-catch-rate-health-healthy')).toBeTruthy();
    expect(healthSpy).toHaveBeenCalledTimes(1);

    const nextPage = screen.getByRole('button', { name: /next page/i });
    await act(async () => {
      fireEvent.click(nextPage);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(missSpy).toHaveBeenCalledWith('proj-1', expect.objectContaining({ page: 1, page_size: 20 }), expect.any(AbortSignal));
    expect(healthSpy).toHaveBeenCalledTimes(1);

    const day30 = screen.getByRole('button', { name: '30d' });
    await act(async () => {
      fireEvent.click(day30);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    expect(healthSpy).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId('sr-catch-rate-health-stale')).toHaveTextContent('snapshot stale (30h)');
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
    expect(spy).toHaveBeenCalledWith('proj-1', expect.objectContaining({ category: 'correctness', page: 0 }), expect.any(AbortSignal));
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

  test('syncs scopeFilter from activeScope prop and resets page on change', async () => {
    const { fetchSrMissedRetrospectives } = await import('../api');
    const spy = fetchSrMissedRetrospectives as ReturnType<typeof vi.fn>;
    spy.mockClear();
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
          activeScope="large"
        />,
      );
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    expect(spy).toHaveBeenCalledWith('proj-1', expect.objectContaining({ scope: 'large', page: 0 }), expect.any(AbortSignal));

    spy.mockClear();

    // Change activeScope — should sync scopeFilter and reset page to 1 (page=0 to the API)
    await act(async () => {
      r.render(
        <SrMissedRetrospectivesPanel
          selectedProjectId="proj-1"
          onSelectTask={() => {}}
          activeScope="small"
        />,
      );
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    expect(spy).toHaveBeenCalledWith('proj-1', expect.objectContaining({ scope: 'small', page: 0 }), expect.any(AbortSignal));
  });

  test('aborted prior request does not surface as error state on rapid filter change', async () => {
    const { fetchSrMissedRetrospectives } = await import('../api');
    const spy = fetchSrMissedRetrospectives as ReturnType<typeof vi.fn>;
    spy.mockClear();

    // First call: never resolves naturally — rejects with AbortError when signal fires
    spy.mockImplementationOnce((_projectId: string, _params: unknown, signal?: AbortSignal) =>
      new Promise((_resolve, reject) => {
        signal?.addEventListener('abort', () => {
          reject(new DOMException('The operation was aborted.', 'AbortError'));
        });
      }),
    );
    // Second call (after filter change): resolves immediately with fresh data
    spy.mockResolvedValueOnce({ items: MOCK_ITEMS, total: 2, task_types: ['create', 'fix'] });

    host = document.createElement('div');
    host.style.width = '800px';
    host.style.height = '900px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <SrMissedRetrospectivesPanel selectedProjectId="proj-1" onSelectTask={() => {}} />,
      );
    });

    // Change filter — this aborts the in-flight first request and issues the second
    await act(async () => {
      const btn30d = screen.getByRole('button', { name: '30d' });
      btn30d.click();
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    // The error state must NOT be visible — AbortError is silenced
    expect(screen.queryByText('Error fetching SR data')).toBeNull();
    // Fresh data from the second fetch must be rendered
    expect(screen.getByText('feat: add OAuth2 PKCE flow with refresh token rotation and session expiry')).toBeTruthy();
  });

  test('stale response from prior filter does not overwrite fresh data', async () => {
    const { fetchSrMissedRetrospectives } = await import('../api');
    const spy = fetchSrMissedRetrospectives as ReturnType<typeof vi.fn>;
    spy.mockClear();

    const FRESH_ITEMS = [{
      task_id: 'fresh-task-1',
      title: 'fresh: only this result should be visible',
      task_type: 'fix',
      scope: 'small',
      created_at: new Date(Date.now() - 86400_000).toISOString(),
      missed_categories: ['correctness'],
      caught_categories: [],
      pr_url: null,
      sr_check_results: {},
      reviewer_findings: [],
    }];

    let resolveStale!: (v: unknown) => void;
    const stalePromise = new Promise(r => { resolveStale = r; });

    // First call returns stale promise (controllable)
    spy.mockReturnValueOnce(stalePromise);
    // Second call (after filter change) resolves immediately with fresh data
    spy.mockResolvedValueOnce({ items: FRESH_ITEMS, total: 1, task_types: ['fix'] });

    host = document.createElement('div');
    host.style.width = '800px';
    host.style.height = '900px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <SrMissedRetrospectivesPanel selectedProjectId="proj-1" onSelectTask={() => {}} />,
      );
    });

    // Change filter — triggers second fetch while first is still in-flight
    await act(async () => {
      const btn30d = screen.getByRole('button', { name: '30d' });
      btn30d.click();
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    // Fresh data must be visible
    expect(screen.getByText('fresh: only this result should be visible')).toBeTruthy();

    // Now resolve the stale promise — it must be discarded
    await act(async () => {
      resolveStale({ items: MOCK_ITEMS, total: 2, task_types: ['create', 'fix'] });
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    // Stale data must NOT have overwritten the fresh data
    expect(screen.queryByText('feat: add OAuth2 PKCE flow with refresh token rotation and session expiry')).toBeNull();
    expect(screen.getByText('fresh: only this result should be visible')).toBeTruthy();
  });

  test('initial render reads srm_ URL params and passes them to fetchSrMissedRetrospectives', async () => {
    window.history.replaceState({}, '', '/?srm_days=30&srm_category=correctness&srm_page=2');

    const { fetchSrMissedRetrospectives } = await import('../api');
    const spy = fetchSrMissedRetrospectives as ReturnType<typeof vi.fn>;
    spy.mockClear();
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
        />,
      );
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    // Should use days=30 from URL, category=correctness, and page=1 (0-indexed: 1)
    expect(spy).toHaveBeenCalledWith('proj-1', expect.objectContaining({ days: 30, category: 'correctness', page: 1 }), expect.any(AbortSignal));
  });

  test('changing days filter writes srm_days to window.location.search', async () => {
    const { fetchSrMissedRetrospectives } = await import('../api');
    const spy = fetchSrMissedRetrospectives as ReturnType<typeof vi.fn>;
    spy.mockClear();
    spy.mockResolvedValue({ items: MOCK_ITEMS, total: 2, task_types: ['create', 'fix'] });

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

    // Click the 30d button
    const btn30d = screen.getByRole('button', { name: '30d' });
    await act(async () => {
      fireEvent.click(btn30d);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    expect(new URLSearchParams(window.location.search).get('srm_days')).toBe('30');
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
    expect(spy).toHaveBeenCalledWith('proj-1', expect.objectContaining({ page: 0 }), expect.any(AbortSignal));
  });

  test('shows amber banner when health status is stale', async () => {
    const api = await import('../api');
    (api.fetchSrCatchRateHealth as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      status: 'stale',
      last_sufficient_computed_at: '2026-05-27T10:00:00Z',
      last_sufficient_age_hours: 26,
      signal_last_computed_at: '2026-05-28T10:00:00Z',
      signal_age_hours: 2,
      data_sufficient: true,
    });

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

    const banner = screen.getByTestId('sr-health-banner');
    expect(banner).toBeTruthy();
    expect(banner.textContent).toContain('stale');
  });

  test('does not show banner when health status is healthy', async () => {
    const api = await import('../api');
    (api.fetchSrCatchRateHealth as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      status: 'healthy',
      last_sufficient_computed_at: '2026-05-28T08:00:00Z',
      last_sufficient_age_hours: 4,
      signal_last_computed_at: '2026-05-28T09:00:00Z',
      signal_age_hours: 3,
      data_sufficient: true,
    });

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

    expect(screen.queryByTestId('sr-health-banner')).toBeNull();
  });

  test('investigation indicator appears on category-frequency badge when activeInvestigationTaskIds has that category', async () => {
    const { fetchSrMissedRetrospectives } = await import('../api');
    const spy = fetchSrMissedRetrospectives as ReturnType<typeof vi.fn>;
    spy.mockClear();
    spy.mockResolvedValueOnce({
      items: MOCK_ITEMS,
      total: 2,
      task_types: ['create', 'fix'],
      category_counts: { correctness: 3, 'error-handling': 1, performance: 2 },
    });

    const onSelectTask = vi.fn();

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
          onSelectTask={onSelectTask}
          activeInvestigationTaskIds={{ correctness: 'inv-task-abc123' }}
        />,
      );
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    // Investigation indicator button must appear for the 'correctness' badge
    const indicator = host.querySelector('[data-testid="investigation-badge-correctness"]');
    if (!indicator) throw new Error('Expected investigation badge indicator for correctness category');
    expect(indicator.getAttribute('title')).toContain('inv-task-abc123');

    // Clicking the indicator should call onSelectTask with the investigation task id
    await act(async () => {
      fireEvent.click(indicator);
    });
    expect(onSelectTask).toHaveBeenCalledWith('inv-task-abc123');

    // No indicator for 'performance' (not in activeInvestigationTaskIds)
    const noIndicator = host.querySelector('[data-testid="investigation-badge-performance"]');
    if (noIndicator) throw new Error('Did not expect investigation badge for performance category');
  });

  test('investigation indicator appears in expanded SrMissRow drawer next to category title', async () => {
    const { fetchSrMissedRetrospectives } = await import('../api');
    const spy = fetchSrMissedRetrospectives as ReturnType<typeof vi.fn>;
    spy.mockClear();
    spy.mockResolvedValueOnce({
      items: MOCK_ITEMS,
      total: 2,
      task_types: ['create', 'fix'],
      category_counts: { correctness: 3, 'error-handling': 1 },
    });

    const onSelectTask = vi.fn();

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
          onSelectTask={onSelectTask}
          activeInvestigationTaskIds={{ correctness: 'inv-task-drawer-xyz' }}
        />,
      );
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    // Expand the first row (task-sr-1 has correctness and error-handling in missed_categories)
    const expandBtns = screen.getAllByRole('button', { name: /^expand /i });
    await act(async () => {
      fireEvent.click(expandBtns[0]);
    });

    // Drawer investigation indicator must appear for correctness
    const drawerIndicator = host.querySelector('[data-testid="investigation-drawer-correctness"]');
    if (!drawerIndicator) throw new Error('Expected drawer investigation indicator for correctness category');
    expect(drawerIndicator.getAttribute('title')).toContain('inv-task-drawer-xyz');

    // Clicking it should call onSelectTask
    await act(async () => {
      fireEvent.click(drawerIndicator);
    });
    expect(onSelectTask).toHaveBeenCalledWith('inv-task-drawer-xyz');

    // No indicator in drawer for error-handling (not in map)
    const noDrawerIndicator = host.querySelector('[data-testid="investigation-drawer-error-handling"]');
    if (noDrawerIndicator) throw new Error('Did not expect drawer indicator for error-handling');
  });

  test('banner dismissed on dismiss click and does not reappear', async () => {
    const api = await import('../api');
    (api.fetchSrCatchRateHealth as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      status: 'frozen',
      last_sufficient_computed_at: '2026-05-26T10:00:00Z',
      last_sufficient_age_hours: 48,
      signal_last_computed_at: '2026-05-28T09:00:00Z',
      signal_age_hours: 3,
      data_sufficient: false,
    });

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

    const banner = screen.getByTestId('sr-health-banner');
    expect(banner).toBeTruthy();

    const dismissBtn = screen.getByRole('button', { name: /dismiss health banner/i });
    await act(async () => {
      fireEvent.click(dismissBtn);
    });

    expect(screen.queryByTestId('sr-health-banner')).toBeNull();
  });
});
