import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { NullBranchDiagnosticsPanel } from './NullBranchDiagnosticsPanel';
import { expectNoOverflowAt, expectFullViewportAt } from '../test/viewport';

const populatedData = {
  projects: [
    { id: 'p1', name: 'A long project name that might wrap on narrow screens', github_owner: 'acme-org', github_repo: 'really-long-repository-name-here', is_self_repo: false, created_at: null },
    { id: 'p2', name: 'Another Project', github_owner: 'acme', github_repo: 'frontend', is_self_repo: false, created_at: null },
  ],
  self_repo_skipped: [
    { id: 'p3', name: 'Blueprint Self', github_owner: 'blueprint', github_repo: 'self', is_self_repo: true, created_at: null },
  ],
  total: 3,
};

vi.mock('../api', () => ({
  fetchNullBranchConfig: vi.fn(async () => populatedData),
  backfillProjectBranches: vi.fn(async () => ({})),
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  authGet: vi.fn(async () => new Response('{}', { status: 200 })),
  API_BASE_URL: '',
}));

describe('NullBranchDiagnosticsPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('no horizontal overflow at 375px with populated project list', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(<NullBranchDiagnosticsPanel />);
      });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    }, { waitMs: 50 });
  });

  test('fully-populated state passes overflow + truncation + touch-target at 375px', async () => {
    await expectFullViewportAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(<NullBranchDiagnosticsPanel />);
      });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    }, { waitMs: 50 });
  });
});
