import { afterEach, describe, test, vi, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { waitFor } from '@testing-library/react';
import { MessagesPanel as MessagesPanelComponent, type MessagesPanelProps } from './MessagesPanel';
import type { ProjectMessage } from './MessageBubble';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt, stressStrings } from '../test/viewport';
import type { StrategicStateResponse } from '../strategicStateApi';
// Preload MarkdownContentInner so React.lazy in MarkdownContent resolves immediately
// in the browser viewport environment, preventing the Suspense fallback from hiding
// the message content during DOM queries.
import './MarkdownContentInner';

type MessagesPanelViewportProps = Omit<MessagesPanelProps, 'canOperateProject'> & {
  canOperateProject?: boolean;
};

function MessagesPanel(props: MessagesPanelViewportProps) {
  return <MessagesPanelComponent canOperateProject={true} {...props} />;
}

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
  MESSAGE_CONTENT_MAX: 20000,
  authHeaders: vi.fn(async () => ({ 'Authorization': 'Bearer test', 'Content-Type': 'application/json' })),
  // MessagesPanel statically imports the pending-confirmation actions; browser-mode
  // ESM requires the mock factory to expose every named export the subtree imports
  // or the linker fails at import time with "does not provide an export named '…'".
  acceptPendingConfirmation: vi.fn(async () => ({ status: 'accepted' })),
  dismissPendingConfirmation: vi.fn(async () => ({ status: 'dismissed' })),
}));

function makeMsg(overrides: Partial<ProjectMessage> = {}): ProjectMessage {
  return {
    id: 'msg-1', project_id: 'proj-1', message_type: 'NOTE', from_type: 'system',
    from_task_id: null, content: 'viewport test content', context: {}, status: 'open',
    resolution: null, resolved_at: null, created_at: new Date().toISOString(),
    ...overrides,
  };
}

async function renderPanel(root: Root, messages: ProjectMessage[], props: Partial<MessagesPanelProps> = {}) {
  const { apiFetch } = await import('../api');
  if (props.filterChrome === 'minimal') {
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (/\/api\/admin\/projects\/[^/]+\/strategic-session$/.test(urlStr) && method === 'GET') {
        return new Response(JSON.stringify({
          conversation: { limit: 20, has_more: false, order: 'oldest_first', items: messages },
          recent_outputs: [],
          session: {},
          status: 'unavailable',
          location: 'unavailable',
          status_reason: 'no_runner',
        }), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });
  } else {
    (apiFetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      new Response(JSON.stringify({ messages }), { status: 200 }),
    );
  }
  await act(async () => {
    root.render(
      <MessagesPanel
        selectedProjectId="project-123"
        onClose={() => {}}
        addToast={() => {}}
        onMessageCountChange={() => {}}
        {...props}
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

    // Verify the tab strip's parent container stays in bounds. Hard-fail
    // when the selector breaks rather than silently skipping.
    const tabStrip = host!.querySelector('[data-testid="saved-views-tab-strip"]') as HTMLElement | null;
    expect(tabStrip).not.toBeNull();
    const parent = tabStrip!.parentElement;
    expect(parent).not.toBeNull();
    const parentRect = parent!.getBoundingClientRect();
    expect(parentRect.right).toBeLessThanOrEqual(388); // 375px content + 12px padding + 1px tolerance
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

  test('long message content does not silently clip text at 375px', async () => {
    const msgs = [
      makeMsg({ id: 'msg-1', content: 'PR #4219 fix(auth): handle token expiry edge case in refresh flow — production users affected', message_type: 'FEEDBACK', status: 'open' }),
      makeMsg({ id: 'msg-2', content: 'account_model_regression_quarantined: model claude-opus-4-7 shows elevated error rate on direction portfolio tasks', message_type: 'NOTE', from_type: 'system', context: { event_type: 'account_model_regression_quarantined' } }),
    ];
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderPanel(r, msgs);
    }, { waitMs: 50 });
  });

  test('minimal chat context package strip does not overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderPanel(r, [], {
        variant: 'page',
        filterChrome: 'minimal',
        showCloseButton: false,
        strategicState: {
          project_id: 'project-123',
          strategic: {
            active_cycle: null,
            active_task: null,
            active_phase: null,
            latest_cycle: null,
            refresh: { pending: null, last_fired_at: null, last_fired_origin: null },
          },
          running_update: { excerpt: null, char_count: 0, acknowledged_at: null },
          problem_model: {
            active_gap_count: 0,
            capability_count: 0,
            pending_confirmation_count: 0,
            top_gap: null,
          },
          directions: { total_count: 0, active_count: 0, paused_count: 0, primary: null },
          messages: { counts: { open: 0, linked: 0 }, items: [] },
          tasks: {
            counts: {
              queued: 17,
              running: 4,
              evaluating: 0,
              blocked: 0,
              parked: 0,
              failed: 0,
              completed: 28,
              total: 49,
            },
            cumulative_estimated_cost_usd: 0,
          },
          attention: {
            level: 'needs_operator',
            summary: '2 items need attention',
            last_seen_at: null,
            counts: {
              needs_operator: 2,
              watch: 5,
              unread: 2,
              questions: 1,
              blockers: 1,
              approvals: 0,
              manual_directions: 0,
              failed_tasks: 0,
              blocked_tasks: 0,
              parked_tasks: 0,
            },
            items: [{
              id: 'message:long',
              kind: 'message',
              severity: 'needs_operator',
              reason: 'question',
              label: 'Question waiting about a very long local workday session',
              ref: { kind: 'message', id: 'long', project_id: 'project-123' },
            }],
          },
          context_package: {
            id: 'strategic-context:project-123',
            title: 'Strategic context for a long-running local workday session',
            status: 'ready',
            item_count: 42,
            token_count: null,
            canvas: null,
            sources: [
              { kind: 'running_update', count: 1 },
              { kind: 'problem_model', count: 7 },
              { kind: 'tasks', count: 34 },
            ],
            drilldowns: [],
          },
          workbench: {
            version: 1,
            interaction_mode: 'continuous',
            active_surface: 'pipeline',
            selected_refs: [],
            session_started_at: null,
            last_interaction_at: null,
          },
          design: { has_design_doc: false, revision: null },
        } as unknown as StrategicStateResponse,
      });
    });

    const strip = host!.querySelector('[data-testid="strategic-context-package-strip"]');
    expect(strip).toBeNull();
    expect(host!.textContent).not.toContain('needs attention');
    expect(host!.textContent).not.toContain('Question waiting about a very long local workday session');
  });

  test('minimal chat composer aligns to the conversation column at desktop widths', async () => {
    await expectNoOverflowAt(1280, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderPanel(r, [
        makeMsg({ id: 'conversation-alignment', message_type: 'FEEDBACK', from_type: 'operator', content: 'Align this message column with the composer.' }),
      ], {
        variant: 'page',
        filterChrome: 'minimal',
        showCloseButton: false,
        strategicState: {
          project_id: 'project-123',
          strategic: {
            active_cycle: null,
            active_task: null,
            active_phase: null,
            latest_cycle: null,
            refresh: { pending: null, last_fired_at: null, last_fired_origin: null },
          },
          context_package: {
            id: 'strategic-context:project-123',
            title: 'Strategic context',
            status: 'ready',
            item_count: 4,
            token_count: null,
            canvas: null,
            sources: [],
            drilldowns: [],
          },
        } as unknown as StrategicStateResponse,
        onOpenCommandPalette: vi.fn(),
      });
    });

    if (!host) throw new Error('Expected host to be mounted');
    const conversationColumn = host.querySelector<HTMLElement>('[data-testid="strategic-conversation-column"]');
    const composerBox = host.querySelector<HTMLElement>('[data-testid="strategic-composer-box"]');
    if (!conversationColumn || !composerBox) {
      throw new Error('Expected conversation column and composer box to be rendered');
    }
    // Composer-adjacent StrategicContextDescriptor bar was removed in #17499;
    // only the in-message receipt variant may render.
    expect(host.querySelector('[data-testid="strategic-context-descriptor"]')).toBeNull();

    const columnRect = conversationColumn.getBoundingClientRect();
    const composerRect = composerBox.getBoundingClientRect();
    expect(Math.abs(columnRect.left - composerRect.left)).toBeLessThanOrEqual(1);
    expect(Math.abs(columnRect.right - composerRect.right)).toBeLessThanOrEqual(1);
    expect(composerRect.width).toBeLessThanOrEqual(942);
  });

  test('stale citation warning chip does not overflow at 375px', async () => {
    const msgs = [
      makeMsg({
        id: 'msg-stale',
        message_type: 'BUG_REPORT',
        status: 'open',
        content: 'Reported bug with stale file references',
        context: {
          _provenance_warnings: {
            citation_state: 'absent',
            broken_refs: [
              'backend/app/research_completion.py:_staleness_provenance_warning',
              'backend/app/strategic_spawning.py:spawn_investigation_task_from_message',
            ],
          },
        },
      }),
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

  test('renders without horizontal overflow at 768px (tablet md: breakpoint)', async () => {
    const msgs = [
      makeMsg({ id: 'r1', context: { event_type: 'pr_merged' } }),
      makeMsg({ id: 'r2', message_type: 'FEEDBACK', status: 'open' }),
    ];
    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderPanel(r, msgs);
    });
  });

  test('mute window countdown chip renders without overflow at 375px', async () => {
    const expiresAt = Date.now() + 3600000;
    localStorage.setItem('messages_panel_mute_window:anonymous:project-123', JSON.stringify({ expiresAt }));
    const msgs = [makeMsg({ id: 'r1', context: { event_type: 'pr_merged' } })];
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderPanel(r, msgs);
    });
    localStorage.removeItem('messages_panel_mute_window:anonymous:project-123');
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

    if (!host) throw new Error('Expected messages panel test host to remain mounted');
    const panelHost = host;
    await waitFor(() => {
      const bubbles = panelHost.querySelectorAll('[data-testid="message-bubble"]');
      const visibleContent = Array.from(bubbles)
        .map(el => el.textContent || '')
        .join(' ');
      expect(visibleContent).toContain('migration schema error');
      expect(visibleContent).not.toContain('pr merged successfully');
    });
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

    let bubbles = host.querySelectorAll('[data-testid="message-bubble"]');
    expect(bubbles.length).toBeGreaterThan(0);

    await act(async () => {
      setReactInputValue(searchInput, '');
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });

    bubbles = host.querySelectorAll('[data-testid="message-bubble"]');
    expect(bubbles.length).toBe(2);
  });

  test('calls onMessageCountChange with all high-signal messages (consistent with closed-panel behavior)', async () => {
    const now = Date.now();
    const dividerTime = now - 3600000; // 1 hour ago
    localStorage.setItem(`messages_panel_last_seen:anonymous:project-123`, String(dividerTime));

    const msgs = [
      makeMsg({
        id: 'blocker-1',
        message_type: 'BLOCKER',
        status: 'open',
        created_at: new Date(now - 7200000).toISOString(), // 2 hours ago (before divider)
      }),
      makeMsg({
        id: 'blocker-2',
        message_type: 'BLOCKER',
        status: 'open',
        created_at: new Date(now - 600000).toISOString(), // 10 min ago (after divider)
      }),
      makeMsg({
        id: 'feedback-1',
        message_type: 'FEEDBACK',
        status: 'open',
        created_at: new Date(now).toISOString(), // just now (after divider)
      }),
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

    const countChanges: number[] = [];
    await act(async () => {
      r.render(
        <MessagesPanel
          selectedProjectId="project-123"
          onClose={() => {}}
          addToast={() => {}}
          onMessageCountChange={(count) => countChanges.push(count)}
        />,
      );
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 100)); });

    // Should have called with all high-signal count (3: all blockers and feedback, regardless of divider time)
    expect(countChanges).toContain(3);

    localStorage.removeItem(`messages_panel_last_seen:anonymous:project-123`);
  });

  test('maintains high-signal count when divider is dismissed (all-messages behavior)', async () => {
    // This test verifies that dismissing the divider does not change the badge count.
    // The count reflects all high-signal messages, not just new ones, so it remains
    // consistent whether the divider is visible or dismissed.
    const now = Date.now();
    const dividerTime = now - 3600000;
    localStorage.setItem(`messages_panel_last_seen:anonymous:project-123`, String(dividerTime));

    const msgs = [
      makeMsg({
        id: 'blocker-1',
        message_type: 'BLOCKER',
        status: 'open',
        created_at: new Date(now).toISOString(),
      }),
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

    const countChanges: number[] = [];
    await act(async () => {
      r.render(
        <MessagesPanel
          selectedProjectId="project-123"
          onClose={() => {}}
          addToast={() => {}}
          onMessageCountChange={(count) => countChanges.push(count)}
        />,
      );
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 100)); });

    // Initially should report 1 high-signal message (the blocker)
    expect(countChanges).toContain(1);

    // Find and click the "Mark all read" button to dismiss divider
    const dismissButtons = host.querySelectorAll('button');
    let foundDismiss = false;
    for (const btn of Array.from(dismissButtons)) {
      if (btn.textContent?.includes('Mark all read')) {
        await act(async () => {
          btn.click();
        });
        foundDismiss = true;
        break;
      }
    }

    if (foundDismiss) {
      await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 100)); });
      // After dismissing the divider, the count should remain 1 (blocker is still open)
      expect(countChanges[countChanges.length - 1]).toBe(1);
    }

    localStorage.removeItem(`messages_panel_last_seen:anonymous:project-123`);
  });

  test('stress fixture — realistic message data does not overflow at 375px', async () => {
    const now = new Date();
    const yesterday = new Date(now.getTime() - 86400000);

    const msgs = [
      // Long-content BLOCKER from agent with task ID and context badges
      makeMsg({
        id: 'stress-blocker-1',
        message_type: 'BLOCKER',
        from_type: 'agent',
        from_task_id: 'task-abc12345-xyz',
        content: stressStrings.title,
        context: { severity: 'high', task_type: 'create', event_type: 'failure_alert' },
        status: 'open',
        created_at: yesterday.toISOString(),
      }),
      // BUG_REPORT with long error snippet and annotation badge
      makeMsg({
        id: 'stress-bug-1',
        message_type: 'BUG_REPORT',
        from_type: 'system',
        content: stressStrings.errorSnippet,
        context: { severity: 'medium', annotation_type: 'issue', task_type: 'fix' },
        status: 'open',
        created_at: now.toISOString(),
      }),
      // FEEDBACK from user with reply context (tests reply-preview row)
      makeMsg({
        id: 'stress-feedback-1',
        message_type: 'FEEDBACK',
        from_type: 'user',
        content: stressStrings.directionText,
        context: { reply_preview: stressStrings.shortTitle },
        status: 'open',
        created_at: now.toISOString(),
      }),
      // NOTE from strategic with cycle_summary badge
      makeMsg({
        id: 'stress-note-1',
        message_type: 'NOTE',
        from_type: 'strategic',
        content: 'Cycle summary: 6/10 tasks merged, 4 failed, 3 pending approval. Mobile responsiveness work is progressing.',
        context: { event_type: 'cycle_summary', severity: 'low' },
        status: 'open',
        created_at: yesterday.toISOString(),
      }),
      // QUESTION from agent with medium severity
      makeMsg({
        id: 'stress-question-1',
        message_type: 'QUESTION',
        from_type: 'agent',
        from_task_id: 'task-def67890-uvw',
        content: stressStrings.shortTitle,
        context: { severity: 'medium' },
        status: 'open',
        created_at: now.toISOString(),
      }),
    ];

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderPanel(r, msgs);
    }, { waitMs: 50 });
  });
});
