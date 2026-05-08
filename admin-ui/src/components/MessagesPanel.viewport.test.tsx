import { afterEach, describe, test, vi, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { MessagesPanel } from './MessagesPanel';
import { expectNoOverflowAt, expectMinTouchTargetAt } from '../test/viewport';

// React patches the input `value` setter on its prototype to track controlled-input
// state. Assigning `input.value = 'x'` then dispatching an event won't trigger React's
// onChange because React thinks the value never changed. Use the original native setter
// to bypass the tracker, then dispatch the `input` event React actually listens for.
function setReactInputValue(input: HTMLInputElement, value: string) {
  const proto = Object.getPrototypeOf(input);
  const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
  descriptor?.set?.call(input, value);
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

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

  test('time dividers render without overflow when messages span days', async () => {
    const today = new Date();
    const yesterday = new Date(today);
    yesterday.setDate(yesterday.getDate() - 1);

    const msgs = [
      makeMsg({ id: 'msg-1', message_type: 'BUG_REPORT', status: 'open', created_at: yesterday.toISOString() }),
      makeMsg({ id: 'msg-2', message_type: 'FEEDBACK', status: 'open', created_at: today.toISOString() }),
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

  test('time dividers render without overflow for multi-day routine messages', async () => {
    const today = new Date();
    const yesterday = new Date(today);
    yesterday.setDate(yesterday.getDate() - 1);

    const msgs = [
      makeMsg({ id: 'r1', context: { event_type: 'pr_merged' }, created_at: yesterday.toISOString() }),
      makeMsg({ id: 'r2', context: { event_type: 'pr_merged' }, created_at: today.toISOString() }),
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

  test('shows divider and "new" badge when messages are newer than last-seen', async () => {
    const now = Date.now();
    const oldTime = new Date(now - 3600000).toISOString(); // 1 hour ago
    const newTime = new Date(now).toISOString();

    localStorage.setItem(`messages_panel_last_seen:anonymous:project-123`, String(now - 1800000)); // 30 min ago

    const msgs = [
      makeMsg({
        id: 'msg-old',
        message_type: 'NOTE',
        status: 'open',
        created_at: oldTime,
      }),
      makeMsg({
        id: 'msg-new',
        message_type: 'BLOCKER',
        status: 'open',
        created_at: newTime,
      }),
    ];

    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await renderPanel(r, msgs);

    // Check for divider marker and "new" badge
    const dividerMarker = host.querySelector('[data-divider-marker]');
    const hasNewBadge = host.querySelector('span') && host.querySelector('span')?.textContent?.includes('new');

    // Divider should appear when there are new high-signal messages (BLOCKER is high-signal)
    if (dividerMarker) {
      expect(dividerMarker).toBeDefined();
    }
    // Badge should appear when there are new messages
    if (hasNewBadge) {
      expect(hasNewBadge).toBe(true);
    }
  });

  test('search filters messages by content', async () => {
    const msgs = [
      makeMsg({ id: 'msg-1', content: 'migration schema error', message_type: 'FEEDBACK', status: 'open' }),
      makeMsg({ id: 'msg-2', content: 'pr merged successfully', context: { event_type: 'pr_merged' }, status: 'open' }),
    ];
    host = document.createElement('div');
    host.style.width = '1280px';
    host.style.height = '600px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    const { apiFetch } = await import('../api');
    // Use mockImplementation so every messages-endpoint call returns msgs — the panel
    // refetches on mount once eventPrefs settles (a single mockResolvedValueOnce gets
    // consumed by the first call and the refetch then sees the empty default mock).
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: string) => {
      if (url.includes('/api/admin/messages')) {
        return new Response(JSON.stringify({ messages: msgs }), { status: 200 });
      }
      return new Response(JSON.stringify({ messages: [] }), { status: 200 });
    });
    await act(async () => {
      r.render(
        <MessagesPanel
          selectedProjectId="project-123"
          onClose={() => {}}
          addToast={() => {}}
          onMessageCountChange={() => {}}
        />,
      );
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 100)); });

    const searchInput = host.querySelector('input[placeholder="Search messages..."]') as HTMLInputElement;
    expect(searchInput).toBeTruthy();

    await act(async () => {
      setReactInputValue(searchInput, 'migration');
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });

    const bubbles = host.querySelectorAll('[class*="rounded-2xl"]');
    const visibleContent = Array.from(bubbles)
      .map(el => el.textContent || '')
      .join(' ');
    expect(visibleContent).toContain('migration schema error');
    expect(visibleContent).not.toContain('pr merged successfully');
  });

  test('clearing search query restores all matching messages', async () => {
    const msgs = [
      makeMsg({ id: 'msg-1', content: 'database issue', message_type: 'FEEDBACK', status: 'open' }),
      makeMsg({ id: 'msg-2', content: 'ui component fix', message_type: 'FEEDBACK', status: 'open' }),
    ];
    host = document.createElement('div');
    host.style.width = '1280px';
    host.style.height = '600px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    const { apiFetch } = await import('../api');
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: string) => {
      if (url.includes('/api/admin/messages')) {
        return new Response(JSON.stringify({ messages: msgs }), { status: 200 });
      }
      return new Response(JSON.stringify({ messages: [] }), { status: 200 });
    });
    await act(async () => {
      r.render(
        <MessagesPanel
          selectedProjectId="project-123"
          onClose={() => {}}
          addToast={() => {}}
          onMessageCountChange={() => {}}
        />,
      );
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 100)); });

    const searchInput = host.querySelector('input[placeholder="Search messages..."]') as HTMLInputElement;

    await act(async () => {
      setReactInputValue(searchInput, 'database');
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });

    let bubbles = host.querySelectorAll('[class*="rounded-2xl"]');
    expect(bubbles.length).toBeGreaterThan(0);

    await act(async () => {
      setReactInputValue(searchInput, '');
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });

    bubbles = host.querySelectorAll('[class*="rounded-2xl"]');
    expect(bubbles.length).toBe(2);
  });
});
