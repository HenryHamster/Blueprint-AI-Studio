import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { fireEvent } from '@testing-library/react';
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
          status: 'NEEDS_REVIEW',
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
          status: 'NEEDS_REVIEW',
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

  test('renders without horizontal overflow at 375px with per-operator lastVisitTimestamp', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.width = '375px';
      host.style.height = '700px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      // Simulate per-operator lastVisitTimestamp that arrives after async auth.
      // Events in the mock are all recent, so all land after this timestamp,
      // exercising the "New since your last visit" divider and Last Visit pill.
      const lastVisit = Date.now() - 48 * 60 * 60 * 1000;
      await act(async () => {
        r.render(
          <ActivityFeed
            selectedProjectId={null}
            refreshInterval={0}
            onSelectTask={() => {}}
            onClose={() => {}}
            lastVisitTimestamp={lastVisit}
          />,
        );
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
      });
    });
  });

  test('search input filters events by title substring (case-insensitive)', async () => {
    host = document.createElement('div');
    host.style.width = '800px';
    host.style.height = '600px';
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

    // Wait for events to load
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 100));
    });

    const searchInput = host?.querySelector(
      'input[placeholder="Search events by title or error..."]',
    ) as HTMLInputElement | null;
    if (!searchInput) {
      throw new Error('Search input not found');
    }
    if (!searchInput.offsetParent) {
      throw new Error('Search input is not visible');
    }

    // Baseline: all four mocked events are rendered before any query is typed.
    const initialText = host?.textContent ?? '';
    if (!initialText.includes('OAuth2 PKCE')) {
      throw new Error('Expected OAuth event in initial render');
    }
    if (!initialText.includes('connection pool exhaustion')) {
      throw new Error('Expected connection-pool event in initial render');
    }
    if (!initialText.includes('Deployed v2.4.1')) {
      throw new Error('Expected deploy event in initial render');
    }

    // Typing a query must update React-controlled state via fireEvent.change.
    // Use a case-mismatched query to also exercise case-insensitive matching.
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: 'OAUTH' } });
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    if (searchInput.value !== 'OAUTH') {
      throw new Error(`Search input value did not update (got "${searchInput.value}")`);
    }

    // Filtering must narrow the rendered list: only the OAuth event should remain.
    const filteredText = host?.textContent ?? '';
    if (!filteredText.includes('OAuth2 PKCE')) {
      throw new Error('OAuth event should remain after filtering');
    }
    if (filteredText.includes('connection pool exhaustion')) {
      throw new Error('Non-matching event "connection pool exhaustion" should be filtered out');
    }
    if (filteredText.includes('Deployed v2.4.1')) {
      throw new Error('Non-matching deploy event should be filtered out');
    }
    if (filteredText.includes('Authentication patterns in microservices')) {
      throw new Error('Non-matching research event should be filtered out');
    }

    // A query with no matches must show the search-specific empty state, not
    // the generic "No recent activity" copy.
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: 'zzz-no-match-zzz' } });
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    const emptyText = host?.textContent ?? '';
    if (!emptyText.includes('No events match your search')) {
      throw new Error('Expected search-specific empty state copy');
    }

    // Clearing the input restores all events.
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: '' } });
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    if (searchInput.value !== '') {
      throw new Error('Search input did not clear');
    }
    const restoredText = host?.textContent ?? '';
    if (!restoredText.includes('connection pool exhaustion')) {
      throw new Error('Clearing the search should restore filtered events');
    }
  });

  test('pending approval pill filters events by status === NEEDS_REVIEW', async () => {
    host = document.createElement('div');
    host.style.width = '800px';
    host.style.height = '600px';
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

    // Wait for events to load
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 100));
    });

    // Baseline: both TASK_COMPLETED and RESEARCH_FINDINGS events with status='NEEDS_REVIEW'
    // plus TASK_FAILED and DEPLOY_EVENT without that status should all be visible
    const initialText = host?.textContent ?? '';
    if (!initialText.includes('OAuth2 PKCE')) {
      throw new Error('Expected OAuth event in initial render');
    }
    if (!initialText.includes('connection pool exhaustion')) {
      throw new Error('Expected connection-pool event in initial render');
    }
    if (!initialText.includes('Deployed v2.4.1')) {
      throw new Error('Expected deploy event in initial render');
    }

    // Click "Pending Approval" pill to filter to NEEDS_REVIEW events
    const pendingPill = Array.from(host?.querySelectorAll('button') ?? []).find(btn =>
      btn.textContent?.includes('Pending Approval'),
    ) as HTMLButtonElement | null;
    if (!pendingPill) {
      throw new Error('Pending Approval pill not found');
    }

    await act(async () => {
      fireEvent.click(pendingPill);
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    // After filtering: only OAuth (evt-1, status=NEEDS_REVIEW) and Research (evt-3, status=NEEDS_REVIEW)
    // should be visible. connection pool (evt-2) and deploy (evt-4) should not be shown.
    const filteredText = host?.textContent ?? '';
    if (!filteredText.includes('OAuth2 PKCE')) {
      throw new Error('OAuth event (NEEDS_REVIEW) should be visible after filtering');
    }
    if (!filteredText.includes('Authentication patterns in microservices')) {
      throw new Error('Research event (NEEDS_REVIEW) should be visible after filtering');
    }
    if (filteredText.includes('connection pool exhaustion')) {
      throw new Error('Non-NEEDS_REVIEW event "connection pool" should be filtered out');
    }
    if (filteredText.includes('Deployed v2.4.1')) {
      throw new Error('Non-NEEDS_REVIEW deploy event should be filtered out');
    }

    // Click "All" pill to restore all events
    const allPill = Array.from(host?.querySelectorAll('button') ?? []).find(btn =>
      btn.textContent?.includes('All') && !btn.textContent?.includes('Pending'),
    ) as HTMLButtonElement | null;
    if (!allPill) {
      throw new Error('All pill not found');
    }

    await act(async () => {
      fireEvent.click(allPill);
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    // After clicking All, all events should be visible again
    const restoredText = host?.textContent ?? '';
    if (!restoredText.includes('OAuth2 PKCE')) {
      throw new Error('OAuth event should be restored after clearing filter');
    }
    if (!restoredText.includes('connection pool exhaustion')) {
      throw new Error('connection pool event should be restored after clearing filter');
    }
    if (!restoredText.includes('Deployed v2.4.1')) {
      throw new Error('Deploy event should be restored after clearing filter');
    }
  });
});
