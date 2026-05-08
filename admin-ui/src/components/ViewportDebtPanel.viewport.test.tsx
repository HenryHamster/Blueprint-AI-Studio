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

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () =>
    new Response(
      JSON.stringify({ rows: MOCK_ROWS, scanned_at: new Date().toISOString() }),
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
});
