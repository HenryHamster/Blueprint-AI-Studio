import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ViewportDebtPanel } from './ViewportDebtPanel';
import { expectNoOverflowAt, expectMinTouchTargetAt } from '../test/viewport';
import { apiFetch } from '../api';

const MOCK_ROWS = [
  {
    component: 'admin-ui/src/components/MessagesPanel.tsx',
    eslint_disable_count: 12,
    data_viewport_allow_small_target: 8,
    data_viewport_allow_overflow: 3,
    data_viewport_allow_truncation: 2,
    total_opt_outs: 25,
    has_viewport_test: true,
    regression_case_count: 2,
    source_url: 'https://github.com/example/repo/blob/main/admin-ui/src/components/MessagesPanel.tsx',
    introducing_pr_number: 4321,
    introducing_commit_sha: 'abc1234def5678',
  },
  {
    component: 'admin-ui/src/components/TaskDetailDrawer.tsx',
    eslint_disable_count: 7,
    data_viewport_allow_small_target: 4,
    data_viewport_allow_overflow: 1,
    data_viewport_allow_truncation: 0,
    total_opt_outs: 12,
    has_viewport_test: false,
    regression_case_count: 0,
    source_url: null,
  },
  {
    component: 'admin-ui/src/components/DirectionsPanel.tsx',
    eslint_disable_count: 5,
    data_viewport_allow_small_target: 2,
    data_viewport_allow_overflow: 0,
    data_viewport_allow_truncation: 0,
    total_opt_outs: 7,
    has_viewport_test: true,
    regression_case_count: 1,
    source_url: 'https://github.com/example/repo/blob/main/admin-ui/src/components/DirectionsPanel.tsx',
  },
];

const MOCK_UNTESTED_ROWS = [
  {
    component: 'admin-ui/src/components/QuickFilterPresets.tsx',
    eslint_disable_count: 0,
    data_viewport_allow_small_target: 0,
    data_viewport_allow_overflow: 0,
    data_viewport_allow_truncation: 0,
    total_opt_outs: 0,
    has_viewport_test: false,
    regression_case_count: 0,
    source_url:
      'https://github.com/example/repo/blob/main/admin-ui/src/components/QuickFilterPresets.tsx',
  },
  {
    component: 'admin-ui/src/components/RunningUpdatePanel.tsx',
    eslint_disable_count: 0,
    data_viewport_allow_small_target: 0,
    data_viewport_allow_overflow: 0,
    data_viewport_allow_truncation: 0,
    total_opt_outs: 0,
    has_viewport_test: false,
    regression_case_count: 0,
    source_url: null,
  },
];

const MOCK_COVERAGE_SUMMARY = {
  total_components: 5,
  untested_count: 3,
  untested_components: [
    'admin-ui/src/components/TaskDetailDrawer.tsx',
    'admin-ui/src/components/QuickFilterPresets.tsx',
    'admin-ui/src/components/RunningUpdatePanel.tsx',
  ],
};

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () =>
    new Response(
      JSON.stringify({
        rows: MOCK_ROWS,
        untested_rows: MOCK_UNTESTED_ROWS,
        scanned_at: new Date().toISOString(),
        coverage_summary: MOCK_COVERAGE_SUMMARY,
      }),
      { status: 200 },
    )
  ),
  authGet: vi.fn(async () => new Response('{}', { status: 200 })),
  API_BASE_URL: '',
}));

describe('ViewportDebtPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) act(() => { r.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('renders populated state without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<ViewportDebtPanel projectId="project-123" />); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
      // Mobile card layout uses inline field labels; their presence confirms the stacked-card
      // path is active (not the wide table that previously forced horizontal scroll).
      const text = host!.textContent ?? '';
      if (!text.includes('MessagesPanel')) throw new Error('component name not rendered in mobile card layout at 375px');
      if (!text.includes('eslint:')) throw new Error('eslint field label not rendered in mobile card layout at 375px');
      if (!text.includes('overflow:')) throw new Error('overflow field label not rendered in mobile card layout at 375px');
      // Confirm the escape hatch is no longer needed — no data-viewport-allow-overflow in this subtree.
      if (host!.querySelector('[data-viewport-allow-overflow]')) {
        throw new Error('data-viewport-allow-overflow escape hatch found — table should use responsive layout at 375px instead');
      }
    });
  });

  test('does not render anchor for javascript: source_url', async () => {
    const xssRows = [
      {
        component: 'admin-ui/src/components/XssTest.tsx',
        eslint_disable_count: 0,
        data_viewport_allow_small_target: 0,
        data_viewport_allow_overflow: 0,
        data_viewport_allow_truncation: 0,
        total_opt_outs: 0,
        has_viewport_test: false,
        regression_case_count: 0,
        source_url: 'javascript:alert(1)',
      },
    ];
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({ rows: xssRows, scanned_at: new Date().toISOString() }),
        { status: 200 },
      )
    );
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => { root?.render(<ViewportDebtPanel projectId="project-xss" />); });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    const anchors = host.querySelectorAll('a[href="javascript:alert(1)"]');
    if (anchors.length > 0) throw new Error('javascript: URL was rendered as an anchor — XSS not blocked');
  });

  test('source links meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<ViewportDebtPanel projectId="project-123" />); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    }, { selector: 'a[aria-label]', waitMs: 50 });
  });

  test('renders untested rows when rows is empty', async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          rows: [],
          untested_rows: MOCK_UNTESTED_ROWS,
          scanned_at: new Date().toISOString(),
          coverage_summary: {
            total_components: 2,
            untested_count: 2,
            untested_components: MOCK_UNTESTED_ROWS.map((r) => r.component),
          },
        }),
        { status: 200 },
      )
    );
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<ViewportDebtPanel projectId="project-untested" />); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
      const panel = host.querySelector('[data-testid="viewport-debt-panel"]');
      if (!panel) throw new Error('panel did not mount when only untested_rows are present');
      const rendered = host.textContent ?? '';
      if (!rendered.includes('QuickFilterPresets')) {
        throw new Error('untested row component name not rendered in table');
      }
      if (!rendered.includes('RunningUpdatePanel')) {
        throw new Error('second untested row component name not rendered in table');
      }
    });
  });

  test('untested-only filter active: no overflow and button meets 44px at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<ViewportDebtPanel projectId="project-123" />); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
      const btn = host.querySelector('button[aria-label]') as HTMLButtonElement | null;
      if (!btn) throw new Error('filter toggle button not found');
      await act(async () => { btn.click(); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 10)); });
    }, { waitMs: 10 });

    // Component still mounted with filter on — check the toggle button meets 44px
    await expectMinTouchTargetAt(375, async () => {}, { selector: 'button[aria-label]', waitMs: 0 });
  });
});
