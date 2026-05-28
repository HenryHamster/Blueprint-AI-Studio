import { afterEach, describe, test, vi, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { CatchUpPanel } from './CatchUpPanel';

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
});
