import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { RecentErrorsWidget } from './RecentErrorsWidget';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async (url: string) => {
    if (url.includes('/recent-orchestrator-errors')) {
      return new Response(
        JSON.stringify({
          buckets: [
            {
              exception_class: 'sqlalchemy.exc.IntegrityError',
              source: 'backend.app.task_lifecycle.handle_task_completion_with_a_long_qualified_name',
              count: 14,
              first_seen_iso: '2026-05-08T10:00:00Z',
              last_seen_iso: '2026-05-08T11:30:00Z',
              sample_message:
                'duplicate key value violates unique constraint "task_pkey" — task already exists when retrying provisioning',
            },
            {
              exception_class: 'TimeoutError',
              source: 'agent.src.handlers.standard.dispatch_with_retry_loop',
              count: 7,
              first_seen_iso: '2026-05-08T10:30:00Z',
              last_seen_iso: '2026-05-08T11:25:00Z',
              sample_message: 'Anthropic API call exceeded 60s budget on streaming completion',
            },
            {
              exception_class: 'KeyError',
              source: 'backend.app.research_completion.parse_findings_payload',
              count: 3,
              first_seen_iso: '2026-05-08T11:00:00Z',
              last_seen_iso: '2026-05-08T11:20:00Z',
              sample_message: '"recommended_fix"',
            },
          ],
          hours: 1,
          total_entries_scanned: 1840,
          generated_at: '2026-05-08T12:00:00Z',
          acknowledged_count: 2,
        }),
        { status: 200 },
      );
    }
    return new Response('{}', { status: 200 });
  }),
}));

describe('RecentErrorsWidget — viewport regression', () => {
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

  test('collapsed state renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<RecentErrorsWidget />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 80));
      });
    });
  });

  test('expanded state renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<RecentErrorsWidget />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 80));
      });
      // Expand the bucket list.
      const expandBtn = document.querySelector<HTMLButtonElement>('button[aria-expanded]');
      if (expandBtn) {
        await act(async () => {
          expandBtn.click();
        });
      }
    });
  });

  test('expanded state renders without silent text truncation at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<RecentErrorsWidget />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 80));
      });
      const expandBtn = document.querySelector<HTMLButtonElement>('button[aria-expanded]');
      if (expandBtn) {
        await act(async () => {
          expandBtn.click();
        });
      }
    });
  });

  test('expanded state renders without horizontal overflow at 768px', async () => {
    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<RecentErrorsWidget />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 80));
      });
      const expandBtn = document.querySelector<HTMLButtonElement>('button[aria-expanded]');
      if (expandBtn) {
        await act(async () => {
          expandBtn.click();
        });
      }
    });
  });
});
