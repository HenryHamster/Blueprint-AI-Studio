import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
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
  getIdTokenAfterAuthRestore: vi.fn(async () => 'test-token'),
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
  fetchSrCatchRateHealth: vi.fn(async () => ({
    status: 'healthy',
    last_sufficient_computed_at: null,
    last_sufficient_age_hours: null,
    signal_last_computed_at: null,
    signal_age_hours: null,
    data_sufficient: true,
  })),
  API_BASE_URL: 'http://localhost:8000',
  authGet: vi.fn(async () => new Response(JSON.stringify({}), { status: 200 })),
  resolveArtifactDownloadUrl: vi.fn(async () => 'https://signed.example.com/artifacts/test.json'),
}));

describe('SrUnhandledEdgesRetrospectivesPanel — viewport regression', () => {
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

  test('aborted prior request does not surface as error state on rapid filter change', async () => {
    const { fetchSrUnhandledEdgesRetrospectives } = await import('../api');
    const spy = fetchSrUnhandledEdgesRetrospectives as ReturnType<typeof vi.fn>;
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
        <SrUnhandledEdgesRetrospectivesPanel selectedProjectId="proj-1" onSelectTask={() => {}} />,
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
    expect(screen.getByText('feat: add retry-on-conflict logic for concurrent task scheduling')).toBeTruthy();
  });

  test('stale response from prior filter does not overwrite fresh data', async () => {
    const { fetchSrUnhandledEdgesRetrospectives } = await import('../api');
    const spy = fetchSrUnhandledEdgesRetrospectives as ReturnType<typeof vi.fn>;
    spy.mockClear();

    const FRESH_ITEMS = [{
      task_id: 'fresh-edge-1',
      title: 'fresh: only this edge result should be visible',
      task_type: 'fix',
      scope: 'small',
      created_at: new Date(Date.now() - 86400_000).toISOString(),
      unhandled_edges: [{ case: 'fresh edge case', handled: false, blind_spot: null, origin: 'reviewer' }],
      pr_url: null,
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
        <SrUnhandledEdgesRetrospectivesPanel selectedProjectId="proj-1" onSelectTask={() => {}} />,
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
    expect(screen.getByText('fresh: only this edge result should be visible')).toBeTruthy();

    // Now resolve the stale promise — it must be discarded
    await act(async () => {
      resolveStale({ items: MOCK_ITEMS, total: 2, task_types: ['create', 'fix'] });
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    // Stale data must NOT have overwritten the fresh data
    expect(screen.queryByText('feat: add retry-on-conflict logic for concurrent task scheduling')).toBeNull();
    expect(screen.getByText('fresh: only this edge result should be visible')).toBeTruthy();
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

    expect(spy).toHaveBeenCalledWith('proj-1', expect.objectContaining({ page: 0 }), expect.any(AbortSignal));
  });

  test('resets pagination to page 0 when a filter changes (days selector)', async () => {
    const { fetchSrUnhandledEdgesRetrospectives } = await import('../api');
    const spy = fetchSrUnhandledEdgesRetrospectives as ReturnType<typeof vi.fn>;
    spy.mockClear();
    // Return total=40 so totalPages=2 and pagination controls are rendered
    spy.mockResolvedValue({ items: MOCK_ITEMS, total: 40, task_types: ['create', 'fix'] });

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

    spy.mockClear();

    // Click the 30d days button to trigger a filter change
    const btn30d = screen.getByRole('button', { name: '30d' });
    await act(async () => {
      fireEvent.click(btn30d);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    // After filter change, panel must re-fetch with page: 0 (pagination reset)
    expect(spy).toHaveBeenCalledWith('proj-1', expect.objectContaining({ page: 0 }), expect.any(AbortSignal));
  });

  test('blind-spot pill bar renders sorted by count, click filters fetch and toggles', async () => {
    const { fetchSrUnhandledEdgesRetrospectives } = await import('../api');
    const spy = fetchSrUnhandledEdgesRetrospectives as ReturnType<typeof vi.fn>;
    spy.mockClear();
    spy.mockResolvedValue({
      items: MOCK_ITEMS,
      total: 2,
      task_types: ['create', 'fix'],
      blind_spot_counts: {
        'partial retry on network timeout boundary': 2,
        'no lock around state mutation that': 5,
        'hook fires unconditionally without checks': 3,
      },
    });

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

    const pillsBar = screen.getByTestId('sr-unhandled-edges-blind-spot-pills');
    const pillButtons = pillsBar.querySelectorAll('button');
    expect(pillButtons.length).toBe(3);
    expect(pillButtons[0].textContent).toContain('no lock around state mutation that');
    expect(pillButtons[0].textContent).toContain('5');
    expect(pillButtons[1].textContent).toContain('hook fires unconditionally without checks');
    expect(pillButtons[2].textContent).toContain('partial retry on network timeout boundary');

    spy.mockClear();
    await act(async () => {
      fireEvent.click(pillButtons[0]);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    expect(spy).toHaveBeenCalledWith(
      'proj-1',
      expect.objectContaining({
        blind_spot: 'no lock around state mutation that',
        page: 0,
      }),
      expect.any(AbortSignal),
    );

    const refreshedPills = screen.getByTestId('sr-unhandled-edges-blind-spot-pills').querySelectorAll('button');
    expect(refreshedPills[0].getAttribute('aria-pressed')).toBe('true');

    spy.mockClear();
    await act(async () => {
      fireEvent.click(refreshedPills[0]);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(spy).toHaveBeenCalledWith(
      'proj-1',
      expect.objectContaining({ blind_spot: undefined, page: 0 }),
      expect.any(AbortSignal),
    );
  });

  test('blind-spot pill bar is hidden when fewer than 2 themes meet the minimum count', async () => {
    const { fetchSrUnhandledEdgesRetrospectives } = await import('../api');
    const spy = fetchSrUnhandledEdgesRetrospectives as ReturnType<typeof vi.fn>;
    spy.mockClear();
    spy.mockResolvedValue({
      items: MOCK_ITEMS,
      total: 2,
      task_types: ['create', 'fix'],
      blind_spot_counts: {
        'no lock around state mutation that': 5,
        'one off blind spot': 1,
      },
    });

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

    expect(screen.queryByTestId('sr-unhandled-edges-blind-spot-pills')).toBeNull();
  });

  test('case-pattern badge bar renders sorted by count, click filters fetch and toggles', async () => {
    const { fetchSrUnhandledEdgesRetrospectives } = await import('../api');
    const spy = fetchSrUnhandledEdgesRetrospectives as ReturnType<typeof vi.fn>;
    spy.mockClear();
    spy.mockResolvedValue({
      items: MOCK_ITEMS,
      total: 2,
      task_types: ['create', 'fix'],
      edge_case_counts: {
        'partial retry on network timeout': 2,
        'concurrent writes race': 5,
        'empty list still triggers hook': 3,
      },
    });

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

    const badgeBar = screen.getByTestId('sr-unhandled-edges-case-pattern-badges');
    const badgeButtons = badgeBar.querySelectorAll('button');
    expect(badgeButtons.length).toBe(3);
    // Sorted by count desc
    expect(badgeButtons[0].textContent).toContain('concurrent writes race');
    expect(badgeButtons[0].textContent).toContain('5');
    expect(badgeButtons[1].textContent).toContain('empty list still triggers hook');
    expect(badgeButtons[2].textContent).toContain('partial retry on network timeout');

    spy.mockClear();
    await act(async () => {
      fireEvent.click(badgeButtons[0]);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    // Clicking a badge sets the case_pattern filter and resets to page 0
    expect(spy).toHaveBeenCalledWith(
      'proj-1',
      expect.objectContaining({
        case_pattern: 'concurrent writes race',
        page: 0,
      }),
      expect.any(AbortSignal),
    );

    // Active badge should be marked selected
    const refreshedBadges = screen.getByTestId('sr-unhandled-edges-case-pattern-badges').querySelectorAll('button');
    expect(refreshedBadges[0].getAttribute('aria-pressed')).toBe('true');

    // Click again to clear the filter
    spy.mockClear();
    await act(async () => {
      fireEvent.click(refreshedBadges[0]);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(spy).toHaveBeenCalledWith(
      'proj-1',
      expect.objectContaining({ case_pattern: undefined, page: 0 }),
      expect.any(AbortSignal),
    );
  });

  test('case-pattern badge bar is hidden when no edge_case_counts present', async () => {
    const { fetchSrUnhandledEdgesRetrospectives } = await import('../api');
    const spy = fetchSrUnhandledEdgesRetrospectives as ReturnType<typeof vi.fn>;
    spy.mockClear();
    // No edge_case_counts in the response → badge bar should not render.
    spy.mockResolvedValue({
      items: MOCK_ITEMS,
      total: 2,
      task_types: ['create', 'fix'],
    });

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

    expect(screen.queryByTestId('sr-unhandled-edges-case-pattern-badges')).toBeNull();
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

  test('filtered-empty state shows "matching <pattern>" when a case-pattern badge is active', async () => {
    const { fetchSrUnhandledEdgesRetrospectives } = await import('../api');
    const spy = fetchSrUnhandledEdgesRetrospectives as ReturnType<typeof vi.fn>;
    spy.mockClear();
    // First load surfaces a badge; clicking it returns an empty filtered result.
    spy
      .mockResolvedValueOnce({
        items: MOCK_ITEMS,
        total: 2,
        task_types: ['create', 'fix'],
        edge_case_counts: { 'concurrent writes race': 3, 'empty list hook': 2 },
      })
      .mockResolvedValue({ items: [], total: 0, task_types: ['create', 'fix'] });

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

    const badgeBar = screen.getByTestId('sr-unhandled-edges-case-pattern-badges');
    const firstBadge = badgeBar.querySelectorAll('button')[0];
    await act(async () => {
      fireEvent.click(firstBadge);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    const emptyEl = screen.getByTestId('sr-unhandled-edges-empty');
    expect(emptyEl).toBeTruthy();
    expect(emptyEl.textContent).toContain('No unhandled edges matching "concurrent writes race"');
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
        <SrUnhandledEdgesRetrospectivesPanel
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

  test('initial render reads sru_ URL params and passes them to fetchSrUnhandledEdgesRetrospectives', async () => {
    window.history.replaceState({}, '', '/?sru_days=30&sru_blind_spot=race&sru_case_pattern=retry&sru_task_type=create&sru_page=2');

    const { fetchSrUnhandledEdgesRetrospectives } = await import('../api');
    const spy = fetchSrUnhandledEdgesRetrospectives as ReturnType<typeof vi.fn>;
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
        <SrUnhandledEdgesRetrospectivesPanel
          selectedProjectId="proj-1"
          onSelectTask={() => {}}
        />,
      );
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    // URL page 2 → API page 1 (backend is 0-indexed); URL days=30 honored; blind_spot/case_pattern/task_type honored
    expect(spy).toHaveBeenCalledWith(
      'proj-1',
      expect.objectContaining({ days: 30, blind_spot: 'race', case_pattern: 'retry', task_type: 'create', page: 1 }),
      expect.any(AbortSignal),
    );
  });

  test('changing days filter writes sru_days to window.location.search', async () => {
    const { fetchSrUnhandledEdgesRetrospectives } = await import('../api');
    const spy = fetchSrUnhandledEdgesRetrospectives as ReturnType<typeof vi.fn>;
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
        <SrUnhandledEdgesRetrospectivesPanel
          selectedProjectId="proj-1"
          onSelectTask={() => {}}
        />,
      );
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    const btn30d = screen.getByRole('button', { name: '30d' });
    await act(async () => {
      fireEvent.click(btn30d);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    expect(new URLSearchParams(window.location.search).get('sru_days')).toBe('30');
  });

  test('switching projects clears sru_ filter state (no leakage across projects)', async () => {
    window.history.replaceState({}, '', '/?sru_days=30&sru_blind_spot=race&sru_case_pattern=retry&sru_page=3');

    const { fetchSrUnhandledEdgesRetrospectives } = await import('../api');
    const spy = fetchSrUnhandledEdgesRetrospectives as ReturnType<typeof vi.fn>;
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
        <SrUnhandledEdgesRetrospectivesPanel
          selectedProjectId="proj-1"
          onSelectTask={() => {}}
        />,
      );
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    spy.mockClear();

    // Switch to a different project — filters must reset to defaults
    await act(async () => {
      r.render(
        <SrUnhandledEdgesRetrospectivesPanel
          selectedProjectId="proj-2"
          onSelectTask={() => {}}
        />,
      );
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    // After project switch: days back to 14, no blind_spot/case_pattern filters, page 0 (UI page 1)
    expect(spy).toHaveBeenCalledWith(
      'proj-2',
      expect.objectContaining({ days: 14, blind_spot: undefined, case_pattern: undefined, page: 0 }),
      expect.any(AbortSignal),
    );

    // URL params for the previous project's filters should be cleared
    const params = new URLSearchParams(window.location.search);
    expect(params.get('sru_days')).toBeNull();
    expect(params.get('sru_blind_spot')).toBeNull();
    expect(params.get('sru_case_pattern')).toBeNull();
    expect(params.get('sru_page')).toBeNull();
  });

  test('malformed sru_ URL params fall back to defaults without throwing', async () => {
    window.history.replaceState({}, '', '/?sru_days=999&sru_page=abc');

    const { fetchSrUnhandledEdgesRetrospectives } = await import('../api');
    const spy = fetchSrUnhandledEdgesRetrospectives as ReturnType<typeof vi.fn>;
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
        <SrUnhandledEdgesRetrospectivesPanel
          selectedProjectId="proj-1"
          onSelectTask={() => {}}
        />,
      );
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    // Malformed days=999 (not in {7,14,30,90}) → defaults to 14; page=abc → defaults to UI page 1 (API page 0)
    expect(spy).toHaveBeenCalledWith(
      'proj-1',
      expect.objectContaining({ days: 14, page: 0 }),
      expect.any(AbortSignal),
    );
  });

  test('banner dismissed on dismiss click and does not reappear', async () => {
    const api = await import('../api');
    (api.fetchSrCatchRateHealth as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      status: 'missing',
      last_sufficient_computed_at: null,
      last_sufficient_age_hours: null,
      signal_last_computed_at: null,
      signal_age_hours: null,
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
        <SrUnhandledEdgesRetrospectivesPanel
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
