import { afterEach, describe, test, vi, expect } from 'vitest';
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

describe('CatchUpPanel', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

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
