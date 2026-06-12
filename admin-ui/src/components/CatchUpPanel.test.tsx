import { afterEach, beforeEach, describe, test, vi, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { CatchUpPanel } from './CatchUpPanel';
import { apiFetch } from '../api';

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api');
  return {
    ...actual,
    apiFetch: vi.fn(async (url: string) => {
      if (url.includes('/digest')) {
        return new Response(JSON.stringify({
          window_hours: 24,
          generated_at: new Date().toISOString(),
          completed_count: 1,
          failed_count: 0,
          pending_approval_count: 0,
          research_cycles: 0,
          prs_opened: 0,
          active_count: 0,
          running_count: 0,
          queued_count: 0,
          active_tasks: [],
          needs_attention: [],
          recent_failures: [],
          recent_completions: [],
          // Required by the shared assertDigestContract guard (backend always emits these arrays).
          direction_progress: [],
          failure_groups: [],
        }), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    }),
    API_BASE_URL: 'http://localhost:8000',
  };
});

const SESSION_GUARD_KEY = 'blueprint-admin:catchUpAutoExpanded';
const COLLAPSED_KEY = 'blueprint-admin:catchUpPanelCollapsed';

const BASE_DIGEST = {
  window_hours: 24,
  generated_at: new Date().toISOString(),
  completed_count: 1,
  failed_count: 0,
  pending_approval_count: 0,
  research_cycles: 0,
  prs_opened: 0,
  active_count: 0,
  running_count: 0,
  queued_count: 0,
  active_tasks: [],
  needs_attention: [],
  recent_failures: [],
  recent_completions: [],
  direction_progress: [],
  failure_groups: [],
};

describe('CatchUpPanel', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    localStorage.removeItem(COLLAPSED_KEY);
    sessionStorage.removeItem(SESSION_GUARD_KEY);
  });

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
    localStorage.removeItem(COLLAPSED_KEY);
    sessionStorage.removeItem(SESSION_GUARD_KEY);
  });

  test('renders catch-up section header', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;

    await act(async () => {
      r.render(
        <CatchUpPanel
          selectedProjectId="project-123"
          lastVisitTimestamp={Date.now() - 24 * 60 * 60 * 1000}
          refreshInterval={60000}
          onSelectTask={() => {}}
          needsAttentionCount={0}
        />,
      );
    });

    await act(async () => {
      await new Promise<void>((resolve) => setTimeout(resolve, 50));
    });

    expect(host?.textContent).toContain('Catch-up');
  });

  test('badge prefers digest aggregate attention count over page-derived needsAttentionCount prop', async () => {
    // Panel is collapsed and the session guard is already set so auto-expand won't fire.
    // needsAttentionCount=2 (page-derived), but the digest returns 3 needs_attention items.
    // After the digest loads the badge must show 3, not 2.
    localStorage.setItem(COLLAPSED_KEY, 'true');
    sessionStorage.setItem(SESSION_GUARD_KEY, 'true');

    const digestWithAttention = {
      ...BASE_DIGEST,
      needs_attention: [
        { task_id: 'a', title: 'Task A', reason: 'failed', error_snippet: null, pr_url: null },
        { task_id: 'b', title: 'Task B', reason: 'failed', error_snippet: null, pr_url: null },
        { task_id: 'c', title: 'Task C', reason: 'approval_required', error_snippet: null, pr_url: null },
      ],
    };
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.includes('/digest')) {
        return new Response(JSON.stringify(digestWithAttention), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    });

    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;

    await act(async () => {
      r.render(
        <CatchUpPanel
          selectedProjectId="project-123"
          lastVisitTimestamp={Date.now() - 24 * 60 * 60 * 1000}
          refreshInterval={60000}
          onSelectTask={() => {}}
          needsAttentionCount={2}
        />,
      );
    });

    await act(async () => {
      await new Promise<void>((resolve) => setTimeout(resolve, 50));
    });

    const text = host?.textContent ?? '';
    expect(text).toContain('3 need attention');
    expect(text).not.toContain('2 need attention');
  });

  test('auto-expand fires once when digest reports attention > 0 on a previously-collapsed panel', async () => {
    // Panel stored as collapsed. Session guard is absent. needsAttentionCount=0 so the
    // synchronous initializer does not expand. Digest loads with 1 needs_attention item →
    // the useEffect should expand the panel and set the session guard.
    localStorage.setItem(COLLAPSED_KEY, 'true');

    const digestWithAttention = {
      ...BASE_DIGEST,
      needs_attention: [
        { task_id: 'x', title: 'Task X', reason: 'failed', error_snippet: null, pr_url: null },
      ],
    };
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.includes('/digest')) {
        return new Response(JSON.stringify(digestWithAttention), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    });

    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;

    await act(async () => {
      r.render(
        <CatchUpPanel
          selectedProjectId="project-123"
          lastVisitTimestamp={Date.now() - 24 * 60 * 60 * 1000}
          refreshInterval={60000}
          onSelectTask={() => {}}
          needsAttentionCount={0}
        />,
      );
    });

    await act(async () => {
      await new Promise<void>((resolve) => setTimeout(resolve, 50));
    });

    // Panel should now be expanded: the attention badge (only visible when collapsed) is gone.
    const text = host?.textContent ?? '';
    expect(text).not.toContain('need attention');
    // The session guard must have been set to prevent a second auto-expand.
    expect(sessionStorage.getItem(SESSION_GUARD_KEY)).toBe('true');
  });

  test('shared session guard prevents a second auto-expand', async () => {
    // Session guard is already set (e.g. by the synchronous initializer on a prior render).
    // Even though the digest reports attention > 0 and the panel is collapsed, the
    // useEffect must not expand again.
    localStorage.setItem(COLLAPSED_KEY, 'true');
    sessionStorage.setItem(SESSION_GUARD_KEY, 'true');

    const digestWithAttention = {
      ...BASE_DIGEST,
      needs_attention: [
        { task_id: 'y', title: 'Task Y', reason: 'failed', error_snippet: null, pr_url: null },
      ],
    };
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.includes('/digest')) {
        return new Response(JSON.stringify(digestWithAttention), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    });

    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;

    await act(async () => {
      r.render(
        <CatchUpPanel
          selectedProjectId="project-123"
          lastVisitTimestamp={Date.now() - 24 * 60 * 60 * 1000}
          refreshInterval={60000}
          onSelectTask={() => {}}
          needsAttentionCount={0}
        />,
      );
    });

    await act(async () => {
      await new Promise<void>((resolve) => setTimeout(resolve, 50));
    });

    // Panel must still be collapsed because the guard prevented auto-expand:
    // the attention badge is visible, showing the digest-derived count of 1.
    const text = host?.textContent ?? '';
    expect(text).toContain('1 need attention');
  });

  test('renders discarded digest diagnostics (migration health, prevention efficacy, release infra)', async () => {
    const digestBody = {
      window_hours: 24,
      generated_at: new Date().toISOString(),
      completed_count: 1,
      failed_count: 0,
      pending_approval_count: 0,
      research_cycles: 0,
      prs_opened: 0,
      active_count: 0,
      running_count: 0,
      queued_count: 0,
      active_tasks: [],
      needs_attention: [],
      recent_failures: [],
      recent_completions: [],
      // Required by the shared assertDigestContract guard (backend always emits these arrays).
      direction_progress: [],
      failure_groups: [],
      migration_health: {
        current_schema_revision: '0096',
        migration_tasks_in_window: 2,
        migration_tasks_succeeded: 1,
        migration_tasks_failed: 1,
        migration_errors_in_window: 0,
        auto_generated_migrations: 3,
      },
      prevention_efficacy: {
        preventive_tasks_created: 10,
        preventive_tasks_merged: 8,
        preventive_tasks_failed: 2,
        preventive_merge_rate: 0.8,
        overall_merge_rate: 0.6,
        assessment: 'positive',
      },
      release_qualifier_infra_metrics: {
        qualifier_runs_total: 5,
        release_infra_failure_total: 2,
        audit_report_missing_count: 1,
        audit_report_parseable_but_unscoreable_count: 0,
        smoketest_report_missing_count: 1,
        release_infra_failure_streak_max: 3,
      },
    };
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.includes('/digest')) {
        return new Response(JSON.stringify(digestBody), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    });

    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;

    await act(async () => {
      r.render(
        <CatchUpPanel
          selectedProjectId="project-123"
          lastVisitTimestamp={Date.now() - 24 * 60 * 60 * 1000}
          refreshInterval={60000}
          onSelectTask={() => {}}
          needsAttentionCount={0}
        />,
      );
    });

    await act(async () => {
      await new Promise<void>((resolve) => setTimeout(resolve, 50));
    });

    const text = host?.textContent ?? '';
    expect(text).toContain('Migration health');
    expect(text).toContain('auto-generated');
    expect(text).toContain('Prevention efficacy');
    expect(text).toContain('Release infra failures');
  });
});
