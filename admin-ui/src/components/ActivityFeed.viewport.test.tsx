import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import ActivityFeed from './ActivityFeed';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  signOut: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
}));

vi.mock('../api', () => ({
  authGet: vi.fn(async () => new Response(JSON.stringify({}), { status: 200 })),
  apiFetch: vi.fn(async () =>
    new Response(
      JSON.stringify([
        {
          id: 'evt-1',
          type: 'TASK_COMPLETED',
          title: 'feat(auth): implement OAuth2 PKCE flow with refresh token rotation',
          timestamp: new Date().toISOString(),
          task_id: 'task-1',
          task_type: 'create',
          commit_hash: 'abc1234',
          repo_url: 'https://github.com/example/repo/commit/abc1234',
          pr_url: 'https://github.com/example/repo/pull/42',
          duration_seconds: 420,
          changed_files: ['auth.ts', 'oauth.ts', 'token.ts'],
          scope: 'medium',
          direction_id: 'dir-abc123',
          review_iterations: 2,
        },
        {
          id: 'evt-2',
          type: 'TASK_FAILED',
          title: 'fix(db): resolve connection pool exhaustion under high concurrency',
          timestamp: new Date(Date.now() - 1000 * 60 * 5).toISOString(),
          task_id: 'task-2',
          failure_class: 'CODE_ERROR',
          taxonomy: 'correctness',
          error_summary:
            'Error: Connection pool exhausted after 30s\n  at Database.connect (/app/db.ts:42:11)\n  at retryWithBackoff (/app/utils.ts:89:5)',
          duration_seconds: 1800,
        },
        {
          id: 'evt-3',
          type: 'RESEARCH_FINDINGS',
          title: 'Research: Authentication patterns in microservices',
          timestamp: new Date(Date.now() - 1000 * 60 * 30).toISOString(),
          finding_count: 7,
        },
        {
          id: 'evt-4',
          type: 'DEPLOY_EVENT',
          title: 'Deployed v2.4.1 to production',
          timestamp: new Date(Date.now() - 1000 * 60 * 60).toISOString(),
          deploy_status: 'STABLE',
        },
      ]),
      { status: 200 },
    ),
  ),
  API_BASE_URL: 'http://localhost:8000',
}));

describe('ActivityFeed — viewport regression', () => {
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
          <ActivityFeed
            selectedProjectId={null}
            refreshInterval={0}
            onSelectTask={() => {}}
            onClose={() => {}}
          />,
        );
      });
      // Wait for API response to load
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
      });
    });
  });
});
