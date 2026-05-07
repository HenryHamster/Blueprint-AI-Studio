import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { MessagesPanel } from './MessagesPanel';
import { expectNoOverflowAt, expectMinTouchTargetAt } from '../test/viewport';

// Mock the api module so MessagesPanel doesn't try to fetch real data
vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => {
    return new Response(JSON.stringify({ messages: [] }), { status: 200 });
  }),
  API_BASE_URL: 'http://localhost:8000',
}));

function makeMsg(overrides: Partial<{
  id: string; message_type: string; from_type: string; context: Record<string, unknown>; status: string; content: string;
}>) {
  return {
    id: 'msg-1', project_id: 'proj-1', message_type: 'NOTE', from_type: 'system',
    from_task_id: null, content: 'viewport test content', context: {}, status: 'open',
    resolution: null, resolved_at: null, created_at: new Date().toISOString(),
    ...overrides,
  };
}

async function renderPanel(root: Root, messages: ReturnType<typeof makeMsg>[]) {
  const { apiFetch } = await import('../api');
  (apiFetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
    new Response(JSON.stringify({ messages }), { status: 200 }),
  );
  await act(async () => {
    root.render(
      <MessagesPanel
        selectedProjectId="project-123"
        onClose={() => {}}
        addToast={() => {}}
        onMessageCountChange={() => {}}
      />,
    );
  });
  await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
}

describe('MessagesPanel — viewport regression', () => {
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

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderPanel(r, []);
    });
  });

  test('routine ops group does not overflow at 375px', async () => {
    const msgs = [
      makeMsg({ id: 'r1', context: { event_type: 'pr_merged' } }),
      makeMsg({ id: 'r2', context: { event_type: 'cycle_summary' } }),
      makeMsg({ id: 'r3', context: { event_type: 'account_model_regression_recovered' } }),
    ];
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderPanel(r, msgs);
    });
  });

  test('routine ops group does not overflow at 1280px', async () => {
    const msgs = [
      makeMsg({ id: 'r1', context: { event_type: 'pr_merged' } }),
      makeMsg({ id: 'r2', context: { event_type: 'cycle_summary' } }),
    ];
    await expectNoOverflowAt(1280, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderPanel(r, msgs);
    });
  });

  test('action buttons meet 44px touch target at 375px', async () => {
    const msgs = [
      makeMsg({ id: 'msg-1', message_type: 'FEEDBACK', status: 'open' }),
      makeMsg({ id: 'msg-2', message_type: 'NOTE', status: 'open' }),
    ];
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderPanel(r, msgs);
    });
  });
});
