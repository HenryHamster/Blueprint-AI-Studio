import { describe, test, expect, vi, afterEach, beforeEach } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { waitFor } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { MessagesPanel as MessagesPanelComponent, type MessagesPanelProps } from './MessagesPanel';

type MessagesPanelTestProps = Omit<MessagesPanelProps, 'canOperateProject'> & {
  canOperateProject?: boolean;
};

function MessagesPanel(props: MessagesPanelTestProps) {
  return <MessagesPanelComponent canOperateProject={true} {...props} />;
}
import type { ProjectMessage } from './MessageBubble';
import type { StrategicStateResponse } from '../strategicStateApi';
import type { StrategicSessionDelta } from '../hooks/useLogViewer';
import {
  enqueueResidentToolHandoff,
  loadQueuedResidentToolHandoffs,
} from '../utils/residentToolHandoff';
import { saveComposerDraft } from '../utils/conversationDraftState';
import { CONVERSATION_INITIAL_WINDOW } from '../hooks/useConversation';

// Full replacement (NOT a partial mock): the real `../firebase` calls
// initializeApp()/getAuth() at module load, which we must not run in jsdom.
// Replacing the whole module is the point — there is no real implementation to
// preserve here, and the only export this suite reads is `auth.currentUser`.
vi.mock('../firebase', () => ({
  auth: { currentUser: { email: 'test-user@example.com' } },
  signInWithGoogle: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
  getIdTokenAfterAuthRestore: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
  signOut: vi.fn(),
}));

// Load-timing telemetry is covered by uiLoadTiming.test.ts. Keep this component
// suite hermetic: a successful auth mock must never turn each render into a
// real request to Vitest's localhost environment URL.
vi.mock('../uiLoadTiming', () => ({
  currentUiSurface: vi.fn(() => 'chat'),
  reportUiLoadTiming: vi.fn(),
}));

// Partial mock — `MessagesPanel.tsx` imports `authHeaders` (and `API_BASE_URL`)
// from '../api' alongside `apiFetch`, and so does its serverSync helper. A
// static factory that only declared `apiFetch`/`API_BASE_URL` left `authHeaders`
// (and any future export) silently `undefined`; preserving the real module via
// importOriginal keeps every transitively-needed name resolving while we still
// stub the network call. Override only `apiFetch`.
//
// `acceptPendingConfirmation`/`dismissPendingConfirmation` are ALSO stubbed:
// their implementations live in `../api/projects`, which imports `apiFetch` from
// `./core` — NOT the `../api` re-export this mock replaces. Without the stub the
// needs-action accept/dismiss handlers would fire a real request (ECONNREFUSED
// in the test environment); mocking the module-level export keeps the handlers
// exercising their real code paths (state updates + refresh) without a network.
vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>();
  return {
    ...actual,
    apiFetch: vi.fn(async () => new Response(JSON.stringify({ messages: [] }), { status: 200 })),
    acceptPendingConfirmation: vi.fn(async () => ({ status: 'accepted', type: 'plan' })),
    dismissPendingConfirmation: vi.fn(async () => {}),
  };
});

import { apiFetch } from '../api';

// jsdom does not implement scrollIntoView; several panel actions invoke it
// (summary card → routine section scroll, showOnlyNew → scroll to first new
// bubble). Stub it module-wide so click handlers do not throw and surface as
// unhandled errors in the vitest run.
if (typeof Element !== 'undefined' && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function () { /* no-op for jsdom */ };
}

// ── apiFetch test helpers ────────────────────────────────────────────────────
// The panel fires several GETs on mount BEFORE the /api/admin/messages fetch
// (last-seen, noise-prefs, temp-mutes, mute-window, saved-views). A naive
// mockResolvedValueOnce({messages}) would feed the messages payload to whichever
// call landed first, leaving the actual messages fetch unfulfilled. A
// mockResolvedValue with a single Response instance also breaks because the
// Response body can only be consumed once. These helpers install URL-aware
// implementations that mint a fresh Response per call.
type _MockMsg = ReturnType<typeof makeMsg>;
function _setupApiFetchMock(opts: {
  messagesQueue?: _MockMsg[][];
  messageTotalsQueue?: number[];
  defaultMessages?: _MockMsg[];
  defaultTotal?: number;
  lastSeenAt?: number | null;
}): void {
  const queue = opts.messagesQueue;
  const totalsQueue = opts.messageTotalsQueue;
  const defaultMessages = opts.defaultMessages ?? [];
  const defaultTotal = opts.defaultTotal ?? defaultMessages.length;
  const lastSeenAt = opts.lastSeenAt === undefined ? null : opts.lastSeenAt;
  let qIdx = 0;
  (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
    const urlStr = String(url);
    const initObj = init as RequestInit | undefined;
    const method = initObj?.method ?? 'GET';
    if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
      return new Response(JSON.stringify({ last_seen_at: lastSeenAt }), { status: 200 });
    }
    if (/\/api\/admin\/projects\/[^/]+\/strategic-session\/messages$/.test(urlStr) && method === 'POST') {
      const body = JSON.parse(String(initObj?.body ?? '{}')) as Record<string, unknown>;
      return new Response(JSON.stringify({
        arming: 'steering',
        wakeup_state: 'queued',
        message: makeMsg({
          id: `sent-${String(body.idempotency_key ?? 'message')}`,
          project_id: 'proj-1',
          from_type: 'user',
          message_type: String(body.message_type ?? 'FEEDBACK') as _MockMsg['message_type'],
          content: String(body.content ?? ''),
          idempotency_key: String(body.idempotency_key ?? ''),
          reply_to_message_id: body.reply_to_message_id ? String(body.reply_to_message_id) : null,
          surface: 'conversation',
        }),
      }), { status: 200 });
    }
    if (/\/api\/admin\/projects\/[^/]+\/strategic-conversation(?:\?|$)/.test(urlStr) && method === 'GET') {
      const msgs = queue
        ? queue[Math.min(qIdx, queue.length - 1)]
        : defaultMessages;
      if (queue) qIdx += 1;
      return new Response(JSON.stringify({
        items: msgs,
        has_more: false,
        next_before_sequence: null,
      }), { status: 200 });
    }
    if (/\/api\/admin\/messages(\?|$)/.test(urlStr) && method === 'GET') {
      const responseIndex = qIdx;
      const msgs = queue
        ? queue[Math.min(responseIndex, queue.length - 1)]
        : defaultMessages;
      if (queue) qIdx += 1;
      const total = totalsQueue?.[Math.min(responseIndex, totalsQueue.length - 1)] ?? defaultTotal;
      return new Response(JSON.stringify({ messages: msgs, total }), { status: 200 });
    }
    // Server-side sync endpoints: GETs are 404'd so the panel falls back to its
    // localStorage state (returning 200/{} would let an empty server response
    // overwrite local prefs, breaking tests that seed prefs via localStorage).
    // PUTs and other writes are silently 200'd.
    if (method === 'GET' && (
      urlStr.includes('/api/admin/messages/noise-prefs') ||
      urlStr.includes('/api/admin/messages/saved-views') ||
      urlStr.includes('/api/admin/messages/temp-mutes') ||
      urlStr.includes('/api/admin/messages/mute-window')
    )) {
      return new Response('', { status: 404 });
    }
    return new Response(JSON.stringify({}), { status: 200 });
  });
}
function _mockMessagesFetch(messages: _MockMsg[], lastSeenAt: number | null = null): void {
  _setupApiFetchMock({ defaultMessages: messages, lastSeenAt });
}
// Seeds last-seen far in the future so the cold-start bootstrap is suppressed,
// no message reads as "new", and the since-last-visit summary card / divider
// don't appear — useful for tests that pre-date those overlays.
function _suppressSinceLastVisit(userEmail: string, projectId: string): void {
  const farFuture = Date.now() + 365 * 24 * 60 * 60 * 1000;
  localStorage.setItem(`messages_panel_last_seen:${userEmail}:${projectId}`, String(farFuture));
}
function _mockMessagesFetchSequence(queue: _MockMsg[][], lastSeenAt: number | null = null): void {
  _setupApiFetchMock({ messagesQueue: queue, lastSeenAt });
}
function _messagesGetCalls(): unknown[][] {
  return vi.mocked(apiFetch).mock.calls.filter((c) => {
    const url = String(c[0]);
    const init = c[1] as RequestInit | undefined;
    return /\/api\/admin\/messages(\?|$)/.test(url) && (!init?.method || init.method === 'GET');
  });
}
function _messagesPostCalls(): unknown[][] {
  return vi.mocked(apiFetch).mock.calls.filter((c) => {
    const url = String(c[0]);
    const init = c[1] as RequestInit | undefined;
    return /\/api\/admin\/messages$/.test(url) && init?.method === 'POST';
  });
}
function _strategicSessionMessagePostCalls(): unknown[][] {
  return vi.mocked(apiFetch).mock.calls.filter((c) => {
    const url = String(c[0]);
    const init = c[1] as RequestInit | undefined;
    return /\/api\/admin\/projects\/[^/]+\/strategic-session\/messages$/.test(url) && init?.method === 'POST';
  });
}

// `makeMsg` is defined below for the component-render describe blocks; it is
// referenced by the helpers above only as a TypeScript value (its return type).
// The forward reference is fine because TypeScript hoists declarations.

// ── Component rendering tests ─────────────────────────────────────────────────

function makeMsg(overrides: Partial<ProjectMessage> = {}): ProjectMessage {
  return {
    id: 'msg-1', project_id: 'proj-1', message_type: 'NOTE', from_type: 'system',
    from_task_id: null, content: 'test content', context: {}, status: 'open',
    resolution: null, resolved_at: null, created_at: new Date().toISOString(),
    ...overrides,
  };
}

function makeStrategicState(overrides: Partial<StrategicStateResponse> = {}): StrategicStateResponse {
  return {
    project_id: 'proj-1',
    strategic: {
      active_cycle: null,
      active_task: null,
      active_phase: null,
      latest_cycle: null,
      refresh: {
        pending: null,
        last_fired_at: null,
        last_fired_origin: null,
      },
    },
    running_update: {
      excerpt: null,
      char_count: 0,
      acknowledged_at: null,
    },
    problem_model: {
      active_gap_count: 0,
      capability_count: 0,
      pending_confirmation_count: 0,
      top_gap: null,
    },
    directions: {
      total_count: 0,
      active_count: 0,
      paused_count: 0,
      primary: null,
    },
    messages: {
      counts: { open: 0, linked: 0 },
      items: [],
    },
    tasks: {
      counts: {
        queued: 5,
        running: 3,
        evaluating: 0,
        blocked: 0,
        parked: 0,
        failed: 0,
        completed: 12,
        total: 20,
      },
      cumulative_estimated_cost_usd: 1.25,
    },
    attention: {
      level: 'needs_operator',
      summary: '1 operator item needs attention',
      last_seen_at: null,
      counts: {
        needs_operator: 1,
        watch: 2,
        unread: 1,
        questions: 1,
        blockers: 0,
        approvals: 0,
        manual_directions: 0,
        failed_tasks: 0,
        blocked_tasks: 0,
        parked_tasks: 0,
      },
      items: [
        {
          id: 'message:msg-1',
          kind: 'message',
          severity: 'needs_operator',
          reason: 'question',
          label: 'Question waiting',
          ref: { kind: 'message', id: 'msg-1', project_id: 'proj-1' },
        },
      ],
    },
    context_package: {
      id: 'strategic-context:proj-1',
      title: 'Workbench context',
      status: 'ready',
      item_count: 18,
      token_count: null,
      canvas: null,
      sources: [],
      drilldowns: [],
    },
    workbench: {
      version: 1,
      interaction_mode: 'check_in',
      active_surface: 'strategic',
      selected_refs: [],
      session_started_at: null,
      last_interaction_at: null,
    },
    design: {
      has_design_doc: false,
      revision: null,
    },
    ...overrides,
  };
}

describe('MessagesPanel chrome modes', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });

  afterEach(() => {
    const r = root;
    if (r) act(() => r.unmount());
    root = null;
    host?.remove();
    host = null;
    localStorage.clear();
    vi.clearAllMocks();
  });

  async function mountPanel(props: Partial<MessagesPanelProps> = {}) {
    host = document.createElement('div');
    host.style.display = 'flex';
    host.style.flexDirection = 'column';
    host.style.height = '600px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <MessagesPanel
          selectedProjectId="proj-1"
          addToast={() => {}}
          onMessageCountChange={() => {}}
          {...props}
        />,
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    return host as HTMLDivElement;
  }

  async function renderPanel(props: Partial<MessagesPanelProps> = {}, messages: _MockMsg[] = []) {
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    _mockMessagesFetch(messages);
    return mountPanel(props);
  }

  function filterToggle(el: HTMLElement): HTMLButtonElement | undefined {
    return [...el.querySelectorAll('button')]
      .find(button => button.title === 'Show filters and saved views' || button.title === 'Hide filters and saved views') as HTMLButtonElement | undefined;
  }

  test('mount fails coverage if load telemetry reaches the real network', async () => {
    const originalFetch = globalThis.fetch;
    const unexpectedFetch = vi.fn((input: RequestInfo | URL): Promise<Response> => {
      throw new Error('Unexpected network request from MessagesPanel: ' + String(input));
    });
    globalThis.fetch = unexpectedFetch as typeof globalThis.fetch;
    try {
      await renderPanel();
      await act(async () => { await Promise.resolve(); });
      expect(unexpectedFetch).not.toHaveBeenCalled();
    } finally {
      globalThis.fetch = originalFetch;
    }
  });


  test('page default omits close chrome and collapses filters', async () => {
    const el = await renderPanel();

    expect(el.querySelector('[aria-label="Close messages panel"]')).toBeNull();
    expect(el.querySelector('input[placeholder="Search messages..."]')).toBeNull();
    expect(el.querySelector('[data-testid="saved-views-tab-strip"]')).toBeNull();

    const toggle = filterToggle(el);
    if (!toggle) throw new Error('filter toggle not found');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');

    await act(async () => { toggle.click(); });

    expect(el.querySelector('input[placeholder="Search messages..."]')).toBeTruthy();
    expect(el.querySelector('[data-testid="saved-views-tab-strip"]')).toBeTruthy();
    expect(filterToggle(el)?.getAttribute('aria-expanded')).toBe('true');
  });

  test('compact message chrome renders one filter command surface without duplicate controls', async () => {
    const el = await renderPanel({ variant: 'page', filterChrome: 'compact' }, [
      makeMsg({ id: 'critical', message_type: 'BLOCKER', from_type: 'agent', content: 'Critical blocker' }),
    ]);

    const collapsedButtons = [...el.querySelectorAll('button')];
    expect(collapsedButtons.filter(button => button.title === 'Show filters and saved views')).toHaveLength(1);
    expect(el.querySelectorAll('input[placeholder="Search messages..."]')).toHaveLength(0);
    expect(el.querySelectorAll('[data-testid="saved-views-tab-strip"]')).toHaveLength(0);

    const toggle = filterToggle(el);
    if (!toggle) throw new Error('filter toggle not found');
    await act(async () => { toggle.click(); });

    const expandedButtons = [...el.querySelectorAll('button')];
    expect(expandedButtons.filter(button => button.title === 'Hide filters and saved views')).toHaveLength(1);
    expect(el.querySelectorAll('input[placeholder="Search messages..."]')).toHaveLength(1);
    expect(el.querySelectorAll('[data-testid="saved-views-tab-strip"]')).toHaveLength(1);
    expect(expandedButtons.filter(button => button.title === 'Toggle sort order')).toHaveLength(1);
    expect(expandedButtons.filter(button => button.title?.includes('Critical only'))).toHaveLength(1);
    expect(expandedButtons.filter(button =>
      button.title?.includes('Only new since') ||
      button.title?.includes('Show all messages') ||
      button.title?.includes('No messages to filter')
    )).toHaveLength(1);
    expect(expandedButtons.filter(button => button.title?.includes('Save current filters'))).toHaveLength(1);
    expect(expandedButtons.filter(button => button.title?.includes('Event type visibility'))).toHaveLength(1);
  });

  test('compact chrome leaves Ctrl+K to the app shortcut without duplicating command controls', async () => {
    const onOpenCommandPalette = vi.fn();
    const el = await renderPanel({ variant: 'page', filterChrome: 'compact', onOpenCommandPalette });
    const textarea = el.querySelector('textarea') as HTMLTextAreaElement | null;
    expect(textarea).not.toBeNull();
    expect(document.activeElement).not.toBe(textarea);

    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true, bubbles: true, cancelable: true }));
    });

    expect(document.activeElement).not.toBe(textarea);
    expect(onOpenCommandPalette).not.toHaveBeenCalled();
    expect(el.querySelectorAll('button[title="Open command palette (Ctrl+K)"]')).toHaveLength(0);
  });

  test('minimal chat composer command button opens the Strategic command surface and focuses composer', async () => {
    const onOpenCommandPalette = vi.fn();
    const el = await renderPanel({ filterChrome: 'minimal', onOpenCommandPalette });
    const textarea = el.querySelector('textarea') as HTMLTextAreaElement | null;
    expect(textarea).not.toBeNull();
    const commandButton = el.querySelector<HTMLButtonElement>('button[title="Open command palette (Ctrl+K)"]');
    expect(commandButton).not.toBeNull();
    expect(el.querySelector('input[placeholder="Search messages..."]')).toBeNull();

    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true, bubbles: true, cancelable: true }));
    });

    expect(document.activeElement).not.toBe(textarea);
    expect(onOpenCommandPalette).not.toHaveBeenCalled();

    await act(async () => {
      commandButton?.click();
    });

    await waitFor(() => expect(document.activeElement).toBe(textarea));
    expect(onOpenCommandPalette).toHaveBeenCalledTimes(1);
  });

  test('empty conversation does not render suggested-message chips', async () => {
    const el = await renderPanel({ filterChrome: 'minimal' });
    const textarea = el.querySelector('textarea') as HTMLTextAreaElement | null;
    if (!textarea) throw new Error('expected minimal composer textarea');

    expect(el.textContent).toContain('Workday can research, plan, and spawn budgeted compute experiments from chat.');
    expect(textarea.value).toBe('');
    expect(el.textContent).not.toContain('What should I research next?');
    expect(el.textContent).not.toContain('Plan a budgeted compute experiment for this idea.');
  });

  test('minimal chat fetches its initial transcript when the Strategic read model is already hydrated', async () => {
    const history = Array.from({ length: 3 }, (_, index) => makeMsg({
      id: `history-${index + 1}`,
      sequence: index + 1,
      content: `History message ${index + 1}`,
      surface: 'conversation',
    }));
    const el = await renderPanel({
      filterChrome: 'minimal',
      strategicState: makeStrategicState(),
    }, history);

    await waitFor(() => expect(el.textContent).toContain('History message 3'));
    const conversationRequests = vi.mocked(apiFetch).mock.calls
      .map(call => String(call[0]))
      .filter(url => url.includes('/strategic-conversation'));
    expect(conversationRequests.some(
      url => url.includes(`limit=${CONVERSATION_INITIAL_WINDOW}`),
    )).toBe(true);
  });

  test('reports chat readiness relative to chat navigation rather than session uptime', async () => {
    const { reportUiLoadTiming } = await import('../uiLoadTiming');
    // Simulate a long-lived page session (500s of uptime) BEFORE the operator
    // ever navigates to chat, then a fast render once chat mounts. The first
    // performance.now() call captures the mount instant; later calls advance
    // by small increments to model the render/effect flush.
    let callCount = 0;
    const nowSpy = vi.spyOn(performance, 'now').mockImplementation(() => {
      callCount += 1;
      return callCount === 1 ? 500_000 : 500_000 + callCount * 5;
    });
    try {
      const history = Array.from({ length: 1 }, (_, index) => makeMsg({
        id: `history-${index + 1}`,
        sequence: index + 1,
        content: `History message ${index + 1}`,
        surface: 'conversation',
      }));
      await renderPanel({
        filterChrome: 'minimal',
        strategicState: makeStrategicState(),
      }, history);

      await waitFor(() => {
        const readyCalls = vi.mocked(reportUiLoadTiming).mock.calls.filter(call => call[2] === 'chat_data_ready');
        expect(readyCalls.length).toBeGreaterThan(0);
      });

      const [, reportedMs, sampleType] = vi.mocked(reportUiLoadTiming).mock.calls
        .find(call => call[2] === 'chat_data_ready') as [string, number, string];
      expect(sampleType).toBe('chat_data_ready');
      // Reported value must be the small post-mount delta, not the raw
      // ~500,000ms absolute performance.now() reading (session uptime).
      expect(reportedMs).toBeLessThan(50_000);
      expect(reportedMs).toBeGreaterThanOrEqual(0);
    } finally {
      nowSpy.mockRestore();
    }
  });

  test('never attributes idle reading time to chat_data_ready after a tab return and project switch', async () => {
    const { reportUiLoadTiming } = await import('../uiLoadTiming');
    // A single controllable clock. The operator returns to an already-loaded
    // chat (no fetch fires — useConversation short-circuits on the unchanged
    // scope), reads for three minutes, then switches project. Every
    // navigation into chat must produce exactly one sample measured from THAT
    // navigation: arming the report on the activation edge and leaving it to
    // fire on the next `loading` toggle produced no sample on the tab return
    // and then attributed the 180s of reading time to the project switch.
    let nowMs = 500_000;
    const nowSpy = vi.spyOn(performance, 'now').mockImplementation(() => nowMs);
    try {
      const history = [makeMsg({
        id: 'history-1',
        sequence: 1,
        content: 'History message 1',
        surface: 'conversation',
      })];
      _suppressSinceLastVisit('test-user@example.com', 'proj-1');
      _mockMessagesFetch(history);
      const props: Partial<MessagesPanelProps> = {
        filterChrome: 'minimal',
        strategicState: makeStrategicState(),
      };
      await mountPanel({ ...props, active: true });
      const r = root;
      if (!r) throw new Error('root not mounted');

      const rerender = async (next: Partial<MessagesPanelProps>) => {
        await act(async () => {
          r.render(
            <MessagesPanel
              selectedProjectId="proj-1"
              addToast={() => {}}
              onMessageCountChange={() => {}}
              {...props}
              {...next}
            />,
          );
        });
        await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
      };

      // Operator leaves chat, comes back 100s later, reads for 180s, then
      // switches project.
      await rerender({ active: false });
      nowMs = 600_000;
      await rerender({ active: true });
      nowMs = 780_000;
      await rerender({
        active: true,
        selectedProjectId: 'proj-2',
        strategicState: makeStrategicState({ project_id: 'proj-2' }),
      });

      const reported = vi.mocked(reportUiLoadTiming).mock.calls
        .filter(call => call[2] === 'chat_data_ready')
        .map(call => call[1] as number);
      // One sample for the initial mount, one for the tab return. A report
      // armed on the activation edge but never fired leaves only the mount
      // sample here — and carries its stale clock into the next navigation.
      expect(reported.length).toBeGreaterThanOrEqual(2);
      // Every sample must be a navigation-relative delta. 100_000 (time since
      // the tab return), 180_000 (idle reading) and 280_000 (time since the
      // last activation) are the inflated readings this guards against.
      for (const ms of reported) expect(ms).toBeLessThan(5_000);
    } finally {
      nowSpy.mockRestore();
    }
  });

  test('returning to chat re-anchors deterministically at newest or at the unread boundary', async () => {
    // The chat surface stays mounted (hidden, not unmounted) across tab
    // switches, so a divider that never renders in conversation mode and a
    // one-shot settle latch both leave re-entry landing wherever the hidden
    // ancestor's scroll happened to sit. This proves the unread boundary is
    // now a real, positioned row in conversation mode — the deterministic
    // anchor target — rather than relying on browser-preserved scroll.
    const seenAt = Date.now() - 3_600_000;
    localStorage.setItem('messages_panel_last_seen:test-user@example.com:proj-1', String(seenAt));
    const older = makeMsg({
      id: 'seen-1', sequence: 1, from_type: 'user', content: 'Already seen message',
      surface: 'conversation', created_at: new Date(seenAt - 60_000).toISOString(),
    });
    const newer = makeMsg({
      id: 'unread-1', sequence: 2, from_type: 'strategic', content: 'Freshly arrived message',
      surface: 'conversation', created_at: new Date(seenAt + 60_000).toISOString(),
    });
    _mockMessagesFetch([older, newer], seenAt);

    const baseProps: Partial<MessagesPanelProps> = {
      selectedProjectId: 'proj-1',
      filterChrome: 'minimal',
      addToast: () => {},
      onMessageCountChange: () => {},
      strategicState: makeStrategicState(),
    };
    const el = await mountPanel({ ...baseProps, active: false });
    const r = root;
    if (!r) throw new Error('root not mounted');
    const rerender = async (next: Partial<MessagesPanelProps>) => {
      await act(async () => {
        r.render(<MessagesPanel {...(baseProps as MessagesPanelProps)} {...next} />);
      });
      await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    };

    // Operator navigates into chat.
    await rerender({ active: true });
    await waitFor(() => {
      expect(el.textContent).toContain('Freshly arrived message');
    });
    const column = () => el.querySelector('[data-testid="strategic-conversation-column"]') as HTMLElement | null;
    expect(column()).toBeTruthy();
    const markerAfterFirstEntry = column()?.querySelector('[data-divider-marker]');
    expect(markerAfterFirstEntry).toBeTruthy();
    expect(markerAfterFirstEntry?.textContent).toContain('NEW since');

    // Operator leaves chat and returns — same message set, a fresh
    // navigation. The boundary must still be present and correctly placed,
    // not lost to a one-shot latch.
    await rerender({ active: false });
    await rerender({ active: true });

    await waitFor(() => {
      expect(el.textContent).toContain('Freshly arrived message');
    });
    const rows = Array.from(
      column()?.querySelectorAll('[data-message-id], [data-divider-marker]') ?? [],
    );
    const seenIdx = rows.findIndex(node => node.getAttribute('data-message-id') === 'seen-1');
    const dividerIdx = rows.findIndex(node => node.hasAttribute('data-divider-marker'));
    const unreadIdx = rows.findIndex(node => node.getAttribute('data-message-id') === 'unread-1');
    expect(seenIdx).toBeGreaterThanOrEqual(0);
    expect(dividerIdx).toBeGreaterThan(seenIdx);
    expect(unreadIdx).toBeGreaterThan(dividerIdx);
  });

  test.each([
    { accessLabel: 'false', canOperateProject: false },
    { accessLabel: 'undefined at runtime', canOperateProject: undefined as never },
  ])('viewer reads the Strategic conversation with access $accessLabel without starting call or mutation surfaces', async ({ canOperateProject }) => {
    const onDismissAlert = vi.fn();
    const onDismissAllAlerts = vi.fn();
    const onStrategicInteraction = vi.fn();
    const message = makeMsg({ content: 'Viewer-readable update', surface: 'conversation' });
    const el = await renderPanel({
      canOperateProject,
      filterChrome: 'minimal',
      strategicState: makeStrategicState(),
      onStrategicInteraction,
      onDismissAlert,
      onDismissAllAlerts,
      alerts: [{
        id: 'alert-1', project_id: 'proj-1', severity: 'WARNING', title: 'Capacity warning',
        message: 'Read-only alert', task_id: null, created_at: new Date().toISOString(),
        dismissed_at: null, acknowledged_at: null,
      }],
    }, [message]);

    await waitFor(() => expect(el.textContent).toContain('Viewer-readable update'));
    expect(el.querySelector('textarea')).toBeNull();
    expect(el.querySelector('button[aria-label="Acknowledge alert"]')).toBeNull();
    expect(el.querySelector('button[title="Dismiss"]')).toBeNull();
    expect(el.textContent).not.toContain('Dismiss all');
    expect(el.textContent).not.toContain('Cancel steer');
    expect(onDismissAlert).not.toHaveBeenCalled();
    expect(onDismissAllAlerts).not.toHaveBeenCalled();
    expect(onStrategicInteraction).not.toHaveBeenCalled();
    expect(vi.mocked(apiFetch).mock.calls.some(([url]) => String(url).includes('/strategic-call'))).toBe(false);
    expect(vi.mocked(apiFetch).mock.calls.some(([, init]) => {
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      return method !== 'GET';
    })).toBe(false);
  });

  test('opens a durable tool handoff as an exact conversation draft', async () => {
    const artifactRef = {
      id: 'tool-ref-7',
      artifact_ref_id: 'tool-ref-7',
      tool_id: 'notes-1',
      project_id: 'proj-1',
      kind: 'notes' as const,
      revision: 7,
      checksum: 'a'.repeat(64),
      author: 'test-user@example.com',
      timestamp: '2026-07-17T00:00:00Z',
      retrieval: {
        capability: 'inline_bounded_text' as const,
        href: '/artifact-refs/tool-ref-7',
      },
    };
    expect(enqueueResidentToolHandoff(
      'test-user@example.com',
      'proj-1',
      {
        projectId: 'proj-1',
        artifactRef,
        intent: 'ask',
        prompt: 'Review Notes revision 7.',
      },
    )).toBe(true);

    const el = await renderPanel({ filterChrome: 'minimal' });

    await waitFor(() => expect(
      (el.querySelector('textarea') as HTMLTextAreaElement).value,
    ).toBe('Review Notes revision 7.'));
    expect(el.querySelector('[aria-label="Remove Notes revision 7"]')).not.toBeNull();
    expect(loadQueuedResidentToolHandoffs(
      'test-user@example.com',
      'proj-1',
    )).toEqual([]);
  });

  test('sends a queued tool revision with operator intent and preserves exact identity across automatic retry', async () => {
    const artifactRef = {
      id: 'tool-ref-retry',
      artifact_ref_id: 'tool-ref-retry',
      tool_id: 'notes-retry',
      project_id: 'proj-1',
      kind: 'notes' as const,
      revision: 12,
      checksum: 'b'.repeat(64),
      author: 'test-user@example.com',
      timestamp: '2026-07-17T00:00:00Z',
      retrieval: {
        capability: 'inline_bounded_text' as const,
        href: '/artifact-refs/tool-ref-retry',
      },
    };
    expect(enqueueResidentToolHandoff('test-user@example.com', 'proj-1', {
      projectId: 'proj-1',
      artifactRef,
      intent: 'ask',
      prompt: 'Review Notes revision 12.',
    })).toBe(true);
    const el = await renderPanel({ filterChrome: 'minimal' });
    await waitFor(() => expect(
      (el.querySelector('textarea') as HTMLTextAreaElement).value,
    ).toBe('Review Notes revision 12.'));

    const baseline = vi.mocked(apiFetch).getMockImplementation();
    if (!baseline) throw new Error('Expected URL-aware apiFetch mock');
    let failFirstSend = true;
    vi.mocked(apiFetch).mockImplementation(async (...args: Parameters<typeof apiFetch>) => {
      const [url, init] = args;
      if (
        failFirstSend
        && /\/api\/admin\/projects\/[^/]+\/strategic-session\/messages$/.test(String(url))
        && init?.method === 'POST'
      ) {
        failFirstSend = false;
        return new Response(JSON.stringify({ detail: 'temporarily unavailable' }), { status: 503 });
      }
      return baseline(...args);
    });

    const send = el.querySelector<HTMLButtonElement>('button[aria-label="Send message"]');
    if (!send) throw new Error('Expected send button');
    await act(async () => { send.click(); });

    // The dropped send parks in needs_retry and recovers on its own — no
    // operator click on "Retry message" required.
    await waitFor(() => expect(_strategicSessionMessagePostCalls()).toHaveLength(2));
    const firstBody = JSON.parse(String(
      (_strategicSessionMessagePostCalls()[0][1] as RequestInit).body ?? '{}',
    ));
    expect(firstBody).toMatchObject({
      content: 'Review Notes revision 12.',
      artifact_refs: [expect.objectContaining({
        artifact_ref_id: 'tool-ref-retry',
        revision: 12,
      })],
      operator_intent: {
        kind: 'ask',
        expected_revision: 12,
      },
    });
    const secondBody = JSON.parse(String(
      (_strategicSessionMessagePostCalls()[1][1] as RequestInit).body ?? '{}',
    ));
    expect(secondBody).toEqual(firstBody);
  });

  test('removing an unrelated artifact preserves the queued handoff operator intent', async () => {
    const unrelated = {
      id: 'ordinary-artifact',
      kind: 'artifact',
      title: 'Run log',
      revision: 3,
    };
    expect(saveComposerDraft('test-user@example.com', 'proj-1', {
      type: 'FEEDBACK',
      content: '',
      reply_to: null,
      source_refs: [],
      artifact_refs: [unrelated],
      attachment_send_key: null,
    })).toBeTruthy();
    const artifactRef = {
      id: 'tool-ref-mixed',
      artifact_ref_id: 'tool-ref-mixed',
      tool_id: 'notes-mixed',
      project_id: 'proj-1',
      kind: 'notes' as const,
      revision: 4,
      checksum: 'e'.repeat(64),
      author: 'test-user@example.com',
      timestamp: '2026-07-17T00:00:00Z',
      retrieval: {
        capability: 'inline_bounded_text' as const,
        href: '/artifact-refs/tool-ref-mixed',
      },
    };
    expect(enqueueResidentToolHandoff('test-user@example.com', 'proj-1', {
      projectId: 'proj-1',
      artifactRef,
      intent: 'ask',
      prompt: 'Review Notes revision 4.',
    })).toBe(true);

    const el = await renderPanel({ filterChrome: 'minimal' });
    await waitFor(() => expect(
      el.querySelector('[aria-label="Remove Notes revision 4"]'),
    ).not.toBeNull());
    const removeUnrelated = el.querySelector<HTMLButtonElement>(
      '[aria-label="Remove Run log revision 3"]',
    );
    if (!removeUnrelated) throw new Error('Expected unrelated artifact chip');
    await act(async () => { removeUnrelated.click(); });

    expect(el.querySelector('[aria-label="Remove Notes revision 4"]')).not.toBeNull();
    const send = el.querySelector<HTMLButtonElement>('button[aria-label="Send message"]');
    if (!send) throw new Error('Expected send button');
    await act(async () => { send.click(); });
    await waitFor(() => expect(_strategicSessionMessagePostCalls()).toHaveLength(1));

    const body = JSON.parse(String(
      (_strategicSessionMessagePostCalls()[0][1] as RequestInit).body ?? '{}',
    ));
    expect(body).toMatchObject({
      artifact_refs: [expect.objectContaining({
        artifact_ref_id: 'tool-ref-mixed',
      })],
      operator_intent: {
        kind: 'ask',
        expected_revision: 4,
        artifact_ref_id: 'tool-ref-mixed',
      },
    });
    expect(body.artifact_refs).toHaveLength(1);
  });

  test('drains only one queued handoff and immediately shows the next after chip removal', async () => {
    const first = {
      id: 'tool-ref-first',
      artifact_ref_id: 'tool-ref-first',
      tool_id: 'notes-first',
      project_id: 'proj-1',
      kind: 'notes' as const,
      revision: 1,
      checksum: 'c'.repeat(64),
      author: 'test-user@example.com',
      timestamp: '2026-07-17T00:00:00Z',
      retrieval: {
        capability: 'inline_bounded_text' as const,
        href: '/artifact-refs/tool-ref-first',
      },
    };
    const second = {
      ...first,
      id: 'tool-ref-second',
      artifact_ref_id: 'tool-ref-second',
      tool_id: 'notes-second',
      revision: 2,
      checksum: 'd'.repeat(64),
      retrieval: {
        capability: 'inline_bounded_text' as const,
        href: '/artifact-refs/tool-ref-second',
      },
    };
    expect(enqueueResidentToolHandoff('test-user@example.com', 'proj-1', {
      projectId: 'proj-1',
      artifactRef: first,
      intent: 'share',
      prompt: 'Share Notes revision 1.',
    })).toBe(true);
    expect(enqueueResidentToolHandoff('test-user@example.com', 'proj-1', {
      projectId: 'proj-1',
      artifactRef: second,
      intent: 'ask',
      prompt: 'Review Notes revision 2.',
    })).toBe(true);

    const el = await renderPanel({ filterChrome: 'minimal' });
    await waitFor(() => expect(el.querySelector(
      '[aria-label="Remove Notes revision 1"]',
    )).not.toBeNull());
    expect(el.querySelector('[aria-label="Remove Notes revision 2"]')).toBeNull();
    expect(loadQueuedResidentToolHandoffs('test-user@example.com', 'proj-1'))
      .toEqual([expect.objectContaining({ artifactRef: second })]);

    const remove = el.querySelector<HTMLButtonElement>(
      '[aria-label="Remove Notes revision 1"]',
    );
    if (!remove) throw new Error('Expected first handoff chip');
    await act(async () => { remove.click(); });

    await waitFor(() => expect(el.querySelector(
      '[aria-label="Remove Notes revision 2"]',
    )).not.toBeNull());
    expect((el.querySelector('textarea') as HTMLTextAreaElement).value)
      .toBe('Review Notes revision 2.');
    expect(loadQueuedResidentToolHandoffs('test-user@example.com', 'proj-1')).toEqual([]);
  });

  test('legacy onClose usage keeps full controls and close action', async () => {
    const onClose = vi.fn();
    const el = await renderPanel({ onClose });

    const closeButton = el.querySelector('[aria-label="Close messages panel"]') as HTMLButtonElement | null;
    if (!closeButton) throw new Error('close button not found');
    expect(el.querySelector('input[placeholder="Search messages..."]')).toBeTruthy();
    expect(el.querySelector('[data-testid="saved-views-tab-strip"]')).toBeTruthy();
    expect(filterToggle(el)).toBeUndefined();

    await act(async () => { closeButton.click(); });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test('page variant uses page chrome even when onClose is provided', async () => {
    const el = await renderPanel({ onClose: vi.fn(), variant: 'page' });

    expect(el.querySelector('[aria-label="Close messages panel"]')).toBeNull();
    expect(el.querySelector('input[placeholder="Search messages..."]')).toBeNull();
    expect(filterToggle(el)?.getAttribute('aria-expanded')).toBe('false');
  });

  test('full message chrome still loads from the generic messages endpoint', async () => {
    await renderPanel({}, [
      makeMsg({ id: 'generic-message', content: 'Generic triage message' }),
    ]);

    expect(_messagesGetCalls().length).toBeGreaterThanOrEqual(1);
  });

  test('reports_server_needs_operator_count_for_badge_in_list_mode', async () => {
    const onMessageCountChange = vi.fn();
    await renderPanel({
      strategicState: makeStrategicState(),
      onMessageCountChange,
    }, [
      makeMsg({ id: 'blocker', message_type: 'BLOCKER' }),
      makeMsg({ id: 'feedback', message_type: 'FEEDBACK' }),
    ]);

    await waitFor(() => expect(onMessageCountChange).toHaveBeenLastCalledWith(1));
  });

  test('keeps reporting server needs_operator count across a poll cycle in list mode', async () => {
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    // The initial load and the subsequent poll return the SAME messages, so
    // messageListsEqual short-circuits setMessages and the memoized
    // highSignalCount (2) never changes — this is the exact path where the
    // reporting effect does not re-run. If the list-mode fetchMessages count
    // update were unguarded it would overwrite the badge with highSignalCount (2)
    // on the poll; the strategic read model must stay the single count authority.
    _mockMessagesFetchSequence([
      [makeMsg({ id: 'blocker', message_type: 'BLOCKER' }), makeMsg({ id: 'feedback', message_type: 'FEEDBACK' })],
      [makeMsg({ id: 'blocker', message_type: 'BLOCKER' }), makeMsg({ id: 'feedback', message_type: 'FEEDBACK' })],
    ]);
    const onMessageCountChange = vi.fn();

    await mountPanel({
      strategicState: makeStrategicState(),
      onMessageCountChange,
      realtimeRefreshSignal: 0,
    });
    await waitFor(() => expect(onMessageCountChange).toHaveBeenLastCalledWith(1));
    const callsBefore = _messagesGetCalls().length;

    // Bump the realtime refresh signal to force a poll with unchanged messages.
    await act(async () => {
      root?.render(
        <MessagesPanel
          selectedProjectId="proj-1"
          addToast={() => {}}
          onMessageCountChange={onMessageCountChange}
          strategicState={makeStrategicState()}
          realtimeRefreshSignal={1}
        />,
      );
    });
    await waitFor(() => expect(_messagesGetCalls().length).toBeGreaterThan(callsBefore));

    // The high-signal fallback (2) must never win: the badge stays at the
    // canonical needs_operator count (1) even after a poll that leaves the
    // message list unchanged.
    expect(onMessageCountChange).not.toHaveBeenCalledWith(2);
    expect(onMessageCountChange).toHaveBeenLastCalledWith(1);
  });

  test('realtime refresh signal immediately reloads generic messages', async () => {
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    _mockMessagesFetchSequence([
      [makeMsg({ id: 'initial-message', message_type: 'BLOCKER', from_type: 'agent', content: 'Initial message copy' })],
      [makeMsg({ id: 'realtime-message', message_type: 'BLOCKER', from_type: 'agent', content: 'Realtime message copy' })],
    ]);
    const addToast = vi.fn();
    const onMessageCountChange = vi.fn();

    const el = await mountPanel({ addToast, onMessageCountChange, realtimeRefreshSignal: 0 });
    await waitFor(() => expect(el.textContent).toContain('Initial message copy'));
    const callsBefore = _messagesGetCalls().length;

    await act(async () => {
      root?.render(
        <MessagesPanel
          selectedProjectId="proj-1"
          addToast={addToast}
          onMessageCountChange={onMessageCountChange}
          realtimeRefreshSignal={1}
        />,
      );
    });

    await waitFor(() => expect(_messagesGetCalls().length).toBeGreaterThan(callsBefore));
    await waitFor(() => expect(el.textContent).toContain('Realtime message copy'));
  });

  test('realtime refresh applies same-id message content updates', async () => {
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    _mockMessagesFetchSequence([
      [makeMsg({ id: 'same-message', message_type: 'BLOCKER', from_type: 'agent', content: 'Original message copy' })],
      [makeMsg({ id: 'same-message', message_type: 'BLOCKER', from_type: 'agent', content: 'Updated message copy' })],
    ]);
    const addToast = vi.fn();
    const onMessageCountChange = vi.fn();

    const el = await mountPanel({ addToast, onMessageCountChange, realtimeRefreshSignal: 0 });
    await waitFor(() => expect(el.textContent).toContain('Original message copy'));

    await act(async () => {
      root?.render(
        <MessagesPanel
          selectedProjectId="proj-1"
          addToast={addToast}
          onMessageCountChange={onMessageCountChange}
          realtimeRefreshSignal={1}
        />,
      );
    });

    await waitFor(() => expect(el.textContent).toContain('Updated message copy'));
    expect(el.textContent).not.toContain('Original message copy');
  });

  test('late polling responses do not overwrite fresher realtime message loads', async () => {
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    const messageResolvers: Array<(response: Response) => void> = [];
    vi.mocked(apiFetch).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const initObj = init as RequestInit | undefined;
      const method = initObj?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (/\/api\/admin\/messages(\?|$)/.test(urlStr) && method === 'GET') {
        return new Promise<Response>(resolve => { messageResolvers.push(resolve); });
      }
      if (method === 'GET' && (
        urlStr.includes('/api/admin/messages/noise-prefs') ||
        urlStr.includes('/api/admin/messages/saved-views') ||
        urlStr.includes('/api/admin/messages/temp-mutes') ||
        urlStr.includes('/api/admin/messages/mute-window')
      )) {
        return new Response('', { status: 404 });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });
    const addToast = vi.fn();
    const onMessageCountChange = vi.fn();

    const el = await mountPanel({ addToast, onMessageCountChange, realtimeRefreshSignal: 0 });
    await waitFor(() => expect(messageResolvers).toHaveLength(1));

    await act(async () => {
      root?.render(
        <MessagesPanel
          selectedProjectId="proj-1"
          addToast={addToast}
          onMessageCountChange={onMessageCountChange}
          realtimeRefreshSignal={1}
        />,
      );
    });
    await waitFor(() => expect(messageResolvers).toHaveLength(2));

    await act(async () => {
      messageResolvers[1](new Response(JSON.stringify({
        messages: [makeMsg({ id: 'fresh-message', message_type: 'BLOCKER', from_type: 'agent', content: 'Fresh realtime message' })],
      }), { status: 200 }));
      await Promise.resolve();
    });
    await waitFor(() => expect(el.textContent).toContain('Fresh realtime message'));

    await act(async () => {
      messageResolvers[0](new Response(JSON.stringify({
        messages: [makeMsg({ id: 'stale-message', message_type: 'BLOCKER', from_type: 'agent', content: 'Stale polling message' })],
      }), { status: 200 }));
      await Promise.resolve();
    });

    expect(el.textContent).toContain('Fresh realtime message');
    expect(el.textContent).not.toContain('Stale polling message');
  });

  test('test_removed_context_bar_module_and_props_stay_absent', async () => {
    const el = await renderPanel({
      filterChrome: 'minimal',
    });

    expect(el.querySelector('[data-testid="strategic-context-package-strip"]')).toBeNull();
    expect(el.querySelector('[data-testid="strategic-context-descriptor"]')).toBeNull();

    const panelSource = readFileSync(path.join(process.cwd(), 'src', 'components', 'MessagesPanel.tsx'), 'utf8');
    expect(panelSource).not.toContain('contextPackage?:');
    expect(panelSource).not.toContain('onOpenStrategicContextCommands');
    expect(panelSource).not.toContain('onStrategicRefresh');
    const sessionHookSource = readFileSync(path.join(process.cwd(), 'src', 'hooks', 'useStrategicSession.ts'), 'utf8');
    expect(sessionHookSource).not.toContain('chatComposerContextDescriptor');
    expect(sessionHookSource).not.toContain('buildStrategicContextDescriptor');
  });

  test('minimal chat chrome keeps Strategic work-session and attention status out of Chat', async () => {
    const el = await renderPanel({
      filterChrome: 'minimal',
      strategicState: makeStrategicState(),
    });

    const strip = el.querySelector('[data-testid="strategic-context-package-strip"]');
    expect(strip).toBeNull();
    expect(el.textContent).not.toContain('needs attention');
    expect(el.textContent).not.toContain('Question waiting');
  });

  test('minimal chat composer no longer renders the composer-adjacent context bar', async () => {
    const sessionStartedAt = new Date(Date.now() - 30 * 60_000).toISOString();
    const lastInteractionAt = new Date(Date.now() - 5 * 60_000).toISOString();
    const onOpenStrategicContextWorkspace = vi.fn();
    const el = await renderPanel({
      filterChrome: 'minimal',
      onOpenStrategicContextWorkspace,
      strategicState: makeStrategicState({
        context_package: {
          id: 'strategic-context:proj-1',
          title: 'Workbench context',
          status: 'ready',
          item_count: 18,
          token_count: null,
          sources: [],
          drilldowns: [],
          resources: [],
          canvas: {
            id: 'strategic-canvas:proj-1',
            node_count: 8,
            edge_count: 7,
            nodes: [],
            edges: [],
          },
        },
        work_session: {
          version: 1,
          id: 'strategic-session:proj-1:2026-06-29T01:00:00Z',
          mode: 'continuous',
          active_surface: 'strategic',
          session_started_at: sessionStartedAt,
          last_interaction_at: lastInteractionAt,
          context_package_id: 'strategic-context:proj-1',
          attention_level: 'needs_operator',
          selected_refs: [
            { kind: 'knowledge_document', id: 'knowledge:kdoc-1', label: 'Operator notes' },
          ],
          selected_ref_descriptors: [
            {
              kind: 'knowledge_document',
              id: 'knowledge:kdoc-1',
              label: 'Operator notes',
              status: 'active',
            },
          ],
          curated_metrics: [],
        },
      }),
      strategicWorkbenchState: {
        version: 1,
        interaction_mode: 'continuous',
        active_surface: 'strategic',
        selected_refs: [
          { kind: 'artifact', id: 'artifact:operator-context-report', label: 'Latest context report' },
        ],
        session_started_at: sessionStartedAt,
        last_interaction_at: lastInteractionAt,
      },
    });

    // The composer-adjacent bar (StrategicContextDescriptor variant="composer")
    // is removed; only an in-message receipt (variant="receipt") may render.
    expect(el.querySelector('[data-testid="strategic-context-descriptor"]')).toBeNull();
    expect(onOpenStrategicContextWorkspace).not.toHaveBeenCalled();
  });

  test('minimal chat composer hides stale Strategic workbench context', async () => {
    const staleNaive = new Date(Date.now() - 5 * 3600_000).toISOString().replace('Z', '');
    const el = await renderPanel({
      filterChrome: 'minimal',
      strategicState: makeStrategicState({
        context_package: null,
        work_session: {
          version: 1,
          id: 'strategic-session:proj-1',
          mode: 'continuous',
          active_surface: 'pipeline',
          session_started_at: staleNaive,
          last_interaction_at: null,
          context_package_id: null,
          attention_level: 'clear',
          selected_refs: [
            { kind: 'artifact', id: 'artifact:stale', label: 'Stale ref' },
          ],
          selected_ref_descriptors: [],
          curated_metrics: [],
        },
      }),
      strategicWorkbenchState: {
        version: 1,
        interaction_mode: 'continuous',
        active_surface: 'pipeline',
        selected_refs: [
          { kind: 'artifact', id: 'artifact:stale', label: 'Stale ref' },
        ],
        session_started_at: staleNaive,
        last_interaction_at: null,
      },
    });

    expect(el.querySelector('[data-testid="strategic-context-descriptor"]')).toBeNull();
    expect(el.textContent).not.toContain('Stale ref');
  });

  test('minimal chat context receipts are passive when useful context is attached', async () => {
    const onOpenStrategicContextWorkspace = vi.fn();
    const operator = makeMsg({
      id: 'operator-context',
      from_type: 'user',
      message_type: 'FEEDBACK',
      content: 'Use this context.',
      context: {
        strategic_session: {
          version: 1,
          interaction_mode: 'continuous',
          active_surface: 'project',
          context_package_id: 'strategic-context:proj-1',
          selected_refs: [
            { kind: 'task', id: 'task-1', label: 'Old task label' },
          ],
          selected_ref_descriptors: [
            {
              kind: 'task',
              id: 'task-1',
              label: 'Review active wave',
              summary: 'Reviewing context descriptors.',
              status: 'IN_PROGRESS',
            },
          ],
          captured_at: '2026-06-29T12:00:00Z',
        },
      },
      created_at: '2026-06-01T10:00:00Z',
    });
    const el = await renderPanel({
      filterChrome: 'minimal',
      onOpenStrategicContextWorkspace,
    }, [operator]);

    const receipt = el.querySelector('[data-testid="strategic-context-receipt"]') as HTMLElement | null;
    expect(receipt).not.toBeNull();
    expect(receipt?.tagName.toLowerCase()).toBe('div');
    expect(receipt?.textContent).toContain('Refs attached');
    expect(receipt?.textContent).toContain('Review active wave');
    await act(async () => {
      receipt?.click();
    });
    expect(onOpenStrategicContextWorkspace).not.toHaveBeenCalled();
  });

  test('minimal chat chrome shows conversation messages chronologically without routine alerts', async () => {
    const routine = makeMsg({
      id: 'routine',
      from_type: 'system',
      message_type: 'NOTE',
      content: 'pr merged successfully',
      context: { event_type: 'pr_merged' },
      created_at: '2026-06-01T10:03:00Z',
    });
    const strategicRoutine = makeMsg({
      id: 'strategic-routine',
      from_type: 'strategic',
      message_type: 'NOTE',
      content: 'cycle summary should stay out of chat',
      context: { event_type: 'cycle_summary' },
      created_at: '2026-06-01T10:04:00Z',
    });
    const strategic = makeMsg({
      id: 'strategic',
      from_type: 'strategist',
      message_type: 'NOTE',
      content: 'Strategic reply',
      created_at: '2026-06-01T10:02:00Z',
    });
    const operator = makeMsg({
      id: 'operator',
      from_type: 'user',
      message_type: 'FEEDBACK',
      content: 'Operator steering',
      created_at: '2026-06-01T10:00:00Z',
    });
    const question = makeMsg({
      id: 'question',
      from_type: 'agent',
      message_type: 'QUESTION',
      content: 'Should I keep this path?',
      created_at: '2026-06-01T10:01:00Z',
    });
    const feedback = makeMsg({
      id: 'feedback',
      from_type: 'agent',
      message_type: 'FEEDBACK',
      content: 'Convert this only in the triage surface.',
      created_at: '2026-06-01T10:05:00Z',
    });
    const blocker = makeMsg({
      id: 'blocker',
      from_type: 'agent',
      message_type: 'BLOCKER',
      content: 'Investigate this only in the triage surface.',
      created_at: '2026-06-01T10:06:00Z',
    });

    const el = await renderPanel({ filterChrome: 'minimal' }, [routine, strategicRoutine, strategic, operator, question, feedback, blocker]);

    const text = el.textContent ?? '';
    expect(text).toContain('Operator steering');
    expect(text).toContain('Should I keep this path?');
    expect(text).toContain('Strategic reply');
    expect(text).toContain('Convert this only in the triage surface.');
    expect(text).toContain('Investigate this only in the triage surface.');
    expect(text).not.toContain('pr merged successfully');
    expect(text).not.toContain('cycle summary should stay out of chat');
    expect(text).not.toContain('routine ops');
    expect(text).not.toContain('Pinned · Blockers');
    expect(text).not.toContain('Awaiting reply · Questions');
    expect(el.querySelector('input[placeholder="Search messages..."]')).toBeNull();
    expect(el.querySelector('[data-testid="saved-views-tab-strip"]')).toBeNull();
    expect(filterToggle(el)).toBeUndefined();
    expect(el.querySelector('button[title="Copy message ref"]')).toBeNull();
    const questionReply = el.querySelector<HTMLButtonElement>('[data-message-id="question"] button[title="Reply"]');
    expect(el.querySelector('[data-message-id="strategic"] button[title="Reply"]')).not.toBeNull();
    expect(questionReply).not.toBeNull();
    expect(el.querySelector('button[title="Resolve"]')).toBeNull();
    expect(el.querySelector('button[title="Delete"]')).toBeNull();
    expect(el.querySelector('button[title="Create Direction"]')).toBeNull();
    expect(el.querySelector('button[title="Investigate"]')).toBeNull();
    expect(text.indexOf('Operator steering')).toBeLessThan(text.indexOf('Should I keep this path?'));
    expect(text.indexOf('Should I keep this path?')).toBeLessThan(text.indexOf('Strategic reply'));

    await act(async () => { questionReply?.click(); });
    expect(el.textContent).toContain('Replying to Agent');
    expect(el.textContent).toContain('Should I keep this path?');
  });

  test('hides the Workday activation trigger but shows its model-generated reply', async () => {
    const activation = makeMsg({
      id: 'workday-activation',
      from_type: 'user',
      message_type: 'NOTE',
      content: 'Internal instruction that must not appear as operator text',
      context: { _resident_activation_prompt: true },
      surface: 'conversation',
      created_at: '2026-06-01T10:00:00Z',
    });
    const opening = makeMsg({
      id: 'workday-opening',
      from_type: 'strategist',
      message_type: 'NOTE',
      content: 'Want to pick up the memory architecture question?',
      surface: 'conversation',
      created_at: '2026-06-01T10:00:01Z',
    });

    const el = await renderPanel(
      { filterChrome: 'minimal' },
      [activation, opening],
    );

    await waitFor(() => {
      expect(el.textContent).toContain(
        'Want to pick up the memory architecture question?',
      );
    });
    expect(el.textContent).not.toContain(
      'Internal instruction that must not appear as operator text',
    );
    expect(el.querySelector('[data-message-id="workday-activation"]')).toBeNull();
    expect(el.querySelector('[data-message-id="workday-opening"]')).not.toBeNull();
  });

  test('full message chrome keeps triage actions available', async () => {
    const feedback = makeMsg({
      id: 'feedback-action',
      from_type: 'agent',
      message_type: 'FEEDBACK',
      content: 'Turn this into a direction.',
      created_at: '2026-06-01T10:00:00Z',
    });
    const blocker = makeMsg({
      id: 'blocker-action',
      from_type: 'agent',
      message_type: 'BLOCKER',
      content: 'Investigate this blocker.',
      created_at: '2026-06-01T10:01:00Z',
    });

    const el = await renderPanel({ filterChrome: 'full' }, [feedback, blocker]);

    expect(el.querySelector('button[title="Create Direction"]')).not.toBeNull();
    expect(el.querySelector('button[title="Investigate"]')).not.toBeNull();
    expect(el.querySelector('button[title="Resolve"]')).toBeNull();
    expect(el.querySelector('button[title="Delete"]')).not.toBeNull();
  });

  test('minimal chat hides operational cycle completion receipts', async () => {
    const el = await renderPanel({
      filterChrome: 'minimal',
      lastCycleCompletion: {
        status: 'completed',
        created_count: 2,
        tasks_spawned: 1,
        running_update_changed: true,
      },
    });

    const text = el.textContent ?? '';
    expect(text).not.toContain('Cycle complete');
    expect(text).not.toContain('+2 directions');
    expect(text).not.toContain('1 task spawned');
    expect(text).not.toContain('running update written');
  });

  test('minimal chat hides operational cancellation and failure receipts', async () => {
    const cancelled = await renderPanel({
      filterChrome: 'minimal',
      lastCycleCompletion: { status: 'cancelled', failure_reason: 'Operator stopped the cycle' },
    });
    expect(cancelled.textContent).not.toContain('Cycle cancelled');
    expect(cancelled.textContent).not.toContain('Operator stopped the cycle');

    const failed = await renderPanel({
      filterChrome: 'minimal',
      lastCycleCompletion: { status: 'failed', failure_reason: 'Execution site unavailable' },
    });
    expect(failed.textContent).not.toContain('Cycle failed');
    expect(failed.textContent).not.toContain('Execution site unavailable');
  });

  test('terminal failure reason stays in operational message chrome only', async () => {
    const conversation = await renderPanel({
      filterChrome: 'minimal',
      lastCycleCompletion: { status: 'failed', failure_reason: 'Execution site unavailable' },
    });
    expect(conversation.textContent).not.toContain('Cycle failed');
    expect(conversation.textContent).not.toContain('Execution site unavailable');

    await act(async () => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;

    const full = await renderPanel({
      filterChrome: 'compact',
      lastCycleCompletion: { status: 'failed', failure_reason: 'Execution site unavailable' },
    });
    expect(full.textContent).toContain('Cycle failed');
    expect(full.textContent).toContain('Execution site unavailable');
  });

  test('minimal chat hides leaked batch action state after switching from full chrome', async () => {
    const first = makeMsg({
      id: 'feedback-a',
      from_type: 'agent',
      message_type: 'FEEDBACK',
      content: 'First selected feedback.',
      created_at: '2026-06-01T10:00:00Z',
    });
    const second = makeMsg({
      id: 'feedback-b',
      from_type: 'agent',
      message_type: 'FEEDBACK',
      content: 'Second selected feedback.',
      created_at: '2026-06-01T10:01:00Z',
    });
    const el = await renderPanel({ filterChrome: 'full' }, [first, second]);
    const selectButton = [...el.querySelectorAll('button')]
      .find(button => button.textContent?.trim() === 'Select') as HTMLButtonElement | undefined;
    if (!selectButton) throw new Error('select button not found');

    await act(async () => { selectButton.click(); });
    expect(el.textContent).toContain('2 selected');
    expect(el.textContent).toContain('Create Directions');

    const mountedRoot = root;
    if (!mountedRoot) throw new Error('panel root not mounted');
    await act(async () => {
      mountedRoot.render(
        <MessagesPanel
          selectedProjectId="proj-1"
          addToast={() => {}}
          onMessageCountChange={() => {}}
          filterChrome="minimal"
        />,
      );
    });
    await waitFor(() => expect(_messagesGetCalls().length).toBeGreaterThanOrEqual(1));

    expect(el.textContent).not.toContain('2 selected');
    expect(el.textContent).not.toContain('Create Directions');
  });

  test('minimal chat hides a stale convert modal after switching from full chrome', async () => {
    const feedback = makeMsg({
      id: 'feedback-convert',
      from_type: 'agent',
      message_type: 'FEEDBACK',
      content: 'Convert me in triage mode.',
      created_at: '2026-06-01T10:00:00Z',
    });
    const el = await renderPanel({ filterChrome: 'full' }, [feedback]);
    const createDirection = el.querySelector('button[title="Create Direction"]') as HTMLButtonElement | null;
    if (!createDirection) throw new Error('create direction button not found');

    await act(async () => { createDirection.click(); });
    expect(el.textContent).toContain('Create direction from message');

    const mountedRoot = root;
    if (!mountedRoot) throw new Error('panel root not mounted');
    await act(async () => {
      mountedRoot.render(
        <MessagesPanel
          selectedProjectId="proj-1"
          addToast={() => {}}
          onMessageCountChange={() => {}}
          filterChrome="minimal"
        />,
      );
    });
    await waitFor(() => expect(_messagesGetCalls().length).toBeGreaterThanOrEqual(1));

    expect(el.textContent).not.toContain('Create direction from message');
    expect(el.querySelector('button[title="Create Direction"]')).toBeNull();
  });

  test('minimal chat target primes reply for an open question and highlights it', async () => {
    const onTargetMessageHandled = vi.fn();
    const scrollIntoView = vi.fn();
    const scrollTo = vi.fn();
    const originalScrollIntoView = Element.prototype.scrollIntoView;
    const originalScrollTo = HTMLElement.prototype.scrollTo;
    Element.prototype.scrollIntoView = scrollIntoView;
    HTMLElement.prototype.scrollTo = scrollTo;
    const question = makeMsg({
      id: 'question-target',
      from_type: 'agent',
      message_type: 'QUESTION',
      content: 'Should I keep the local runner online?',
      status: 'open',
      created_at: '2026-06-01T10:01:00Z',
    });

    try {
      const el = await renderPanel({
        filterChrome: 'minimal',
        targetMessage: { messageId: 'question-target', action: 'reply' },
        onTargetMessageHandled,
      }, [question]);

      await waitFor(() => expect(el.querySelector('[data-message-id="question-target"]')).toBeTruthy());
      await waitFor(() => expect(onTargetMessageHandled).toHaveBeenCalledTimes(1));
      const bubble = el.querySelector('[data-message-id="question-target"]');
      expect(bubble?.className).toContain('ring-1');
      expect(scrollIntoView).toHaveBeenCalled();
      expect(el.textContent).toContain('Replying to Agent');
      expect(el.textContent).toContain('Should I keep the local runner online?');
    } finally {
      Element.prototype.scrollIntoView = originalScrollIntoView;
      HTMLElement.prototype.scrollTo = originalScrollTo;
    }
  });

  test('minimal chat target primes reply for an open Strategic message', async () => {
    const onTargetMessageHandled = vi.fn();
    const scrollIntoView = vi.fn();
    const originalScrollIntoView = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scrollIntoView;
    const strategic = makeMsg({
      id: 'strategic-target',
      from_type: 'strategic',
      message_type: 'NOTE',
      content: 'I can compare those two plans.',
      status: 'open',
      created_at: '2026-06-01T10:01:00Z',
    });

    try {
      const el = await renderPanel({
        filterChrome: 'minimal',
        targetMessage: { messageId: 'strategic-target', action: 'reply' },
        onTargetMessageHandled,
      }, [strategic]);

      await waitFor(() => expect(onTargetMessageHandled).toHaveBeenCalledTimes(1));
      const bubble = el.querySelector('[data-message-id="strategic-target"]');
      expect(bubble?.className).toContain('ring-1');
      expect(scrollIntoView).toHaveBeenCalled();
      expect(el.textContent).toContain('Replying to Strategic');
      expect(el.textContent).toContain('I can compare those two plans.');
    } finally {
      Element.prototype.scrollIntoView = originalScrollIntoView;
    }
  });

  test('minimal chat target keeps non-replyable messages highlight-only', async () => {
    const onTargetMessageHandled = vi.fn();
    const addToast = vi.fn();
    const operator = makeMsg({
      id: 'operator-target',
      from_type: 'user',
      message_type: 'FEEDBACK',
      content: 'I already chose the smaller plan.',
      status: 'open',
      created_at: '2026-06-01T10:01:00Z',
    });

    const el = await renderPanel({
      filterChrome: 'minimal',
      targetMessage: { messageId: 'operator-target', action: 'reply' },
      onTargetMessageHandled,
      addToast,
    }, [operator]);

    await waitFor(() => expect(onTargetMessageHandled).toHaveBeenCalledTimes(1));
    const bubble = el.querySelector('[data-message-id="operator-target"]');
    expect(bubble?.className).toContain('ring-1');
    expect(addToast).toHaveBeenCalledWith('Reply not primed: target must be an open Agent or Strategic message.', 'info');
    expect(el.textContent).not.toContain('Replying to Operator');
    expect(el.querySelector('textarea')?.value).toBe('');
  });

  test('minimal chat target does not prime replies for filtered Strategic routine messages', async () => {
    const onTargetMessageHandled = vi.fn();
    const scrollIntoView = vi.fn();
    const originalScrollIntoView = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scrollIntoView;
    const routine = makeMsg({
      id: 'strategic-routine-target',
      from_type: 'strategic',
      message_type: 'NOTE',
      content: 'cycle summary should stay out of chat',
      context: { event_type: 'cycle_summary' },
      status: 'open',
      created_at: '2026-06-01T10:01:00Z',
    });

    try {
      const el = await renderPanel({
        filterChrome: 'minimal',
        targetMessage: { messageId: 'strategic-routine-target', action: 'reply' },
        onTargetMessageHandled,
      }, [routine]);

      await waitFor(() => expect(onTargetMessageHandled).toHaveBeenCalledTimes(1));
      expect(el.querySelector('[data-message-id="strategic-routine-target"]')).toBeNull();
      expect(scrollIntoView).not.toHaveBeenCalled();
      expect(el.textContent).not.toContain('Replying to Strategic');
      expect(el.textContent).not.toContain('cycle summary should stay out of chat');
      expect(el.querySelector('textarea')?.value).toBe('');
    } finally {
      Element.prototype.scrollIntoView = originalScrollIntoView;
    }
  });

  test('minimal chat question replies use one durable idempotent Resident message POST', async () => {
    const question = makeMsg({
      id: 'question-target',
      from_type: 'agent',
      message_type: 'QUESTION',
      content: 'Should I keep the local runner online?',
      status: 'open',
      created_at: '2026-06-01T10:01:00Z',
    });
    const el = await renderPanel({
      filterChrome: 'minimal',
      targetMessage: { messageId: 'question-target', action: 'reply' },
    }, [question]);

    const { fireEvent } = await import('@testing-library/react');
    await waitFor(() => expect(el.textContent).toContain('Replying to Agent'));
    const textarea = el.querySelector('textarea') as HTMLTextAreaElement;
    const sendButton = [...el.querySelectorAll('button')]
      .find(button => button.getAttribute('aria-label') === 'Send message') as HTMLButtonElement | undefined;
    if (!sendButton) throw new Error('Expected send button');

    await act(async () => {
      fireEvent.change(textarea, { target: { value: 'Keep it online.' } });
    });
    await act(async () => {
      sendButton.click();
    });

    await waitFor(() => expect(_strategicSessionMessagePostCalls()).toHaveLength(1));
    expect(vi.mocked(apiFetch).mock.calls.some(call => (
      String(call[0]).includes('/api/admin/messages/question-target')
      && (call[1] as RequestInit | undefined)?.method === 'PATCH'
    ))).toBe(false);
    const postBody = JSON.parse(String((_strategicSessionMessagePostCalls()[0][1] as RequestInit).body ?? '{}'));
    expect(postBody).toMatchObject({
      content: 'Keep it online.',
      reply_to_message_id: 'question-target',
    });
    expect(postBody.idempotency_key).toBeTruthy();
  });

  test('minimal chat non-question replies send through the Strategic session facade', async () => {
    const strategic = makeMsg({
      id: 'strategic-reply-target',
      from_type: 'strategic',
      message_type: 'NOTE',
      content: 'I can compare those two plans.',
      status: 'open',
      created_at: '2026-06-01T10:01:00Z',
    });
    const el = await renderPanel({
      filterChrome: 'minimal',
      targetMessage: { messageId: 'strategic-reply-target', action: 'reply' },
    }, [strategic]);

    const { fireEvent } = await import('@testing-library/react');
    await waitFor(() => expect(el.textContent).toContain('Replying to Strategic'));
    const textarea = el.querySelector('textarea') as HTMLTextAreaElement;
    const sendButton = [...el.querySelectorAll('button')]
      .find(button => button.getAttribute('aria-label') === 'Send message') as HTMLButtonElement | undefined;
    if (!sendButton) throw new Error('Expected send button');

    await act(async () => {
      fireEvent.change(textarea, { target: { value: 'Compare the smaller plan.' } });
    });
    await act(async () => {
      sendButton.click();
    });

    await waitFor(() => expect(_strategicSessionMessagePostCalls()).toHaveLength(1));
    expect(_messagesPostCalls()).toHaveLength(0);
    const postBody = JSON.parse(String((_strategicSessionMessagePostCalls()[0][1] as RequestInit).body ?? '{}'));
    expect(postBody).toMatchObject({
      message_type: 'FEEDBACK',
      content: 'Compare the smaller plan.',
      reply_to_message_id: 'strategic-reply-target',
    });
  });

  test('minimal chat chrome hides queued and working activity state', async () => {
    const base = makeStrategicState();
    const queuedState = makeStrategicState({
      strategic: {
        ...base.strategic,
        refresh: {
          pending: {
            requested_at: '2026-06-01T10:00:00Z',
            origin: 'operator',
            reason: 'operator_message',
            count: 1,
          },
          last_fired_at: null,
          last_fired_origin: null,
        },
      },
    });
    const queuedEl = await renderPanel({
      filterChrome: 'minimal',
      strategicState: queuedState,
    });

    const queuedStrip = queuedEl.querySelector('[data-testid="strategic-context-package-strip"]');
    expect(queuedStrip).toBeNull();
    expect(queuedEl.textContent).not.toContain('Strategic steer queued');
    expect(queuedEl.textContent).not.toContain('Cancel');

    await act(async () => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;

    const workingEl = await renderPanel({
      filterChrome: 'minimal',
      strategicState: makeStrategicState(),
      strategistActive: true,
      cycleProgress: { task_id: 't1', phase_label: 'Reviewing context' },
    });
    const workingStrip = workingEl.querySelector('[data-testid="strategic-context-package-strip"]');
    expect(workingStrip).toBeNull();
    expect(workingEl.textContent).not.toContain('Strategist: Reviewing context');
    expect(workingEl.textContent).not.toContain('Strategic is working');
  });

  test('minimal chat chrome hides recent steer receipts', async () => {
    const base = makeStrategicState();
    const state = makeStrategicState({
      strategic: {
        ...base.strategic,
        refresh: {
          pending: null,
          last_fired_at: new Date(Date.now() - 5 * 60_000).toISOString(),
          last_fired_origin: 'operator',
        },
      },
    });

    const el = await renderPanel({
      filterChrome: 'minimal',
      strategicState: state,
    });

    const strip = el.querySelector('[data-testid="strategic-context-package-strip"]');
    expect(strip).toBeNull();
    expect(el.textContent).not.toContain('strategic cycle ran');
  });

  test('compact/full message chrome keeps the context package out of the legacy surface', async () => {
    const el = await renderPanel({
      filterChrome: 'compact',
    });

    expect(el.querySelector('[data-testid="strategic-context-package-strip"]')).toBeNull();
  });

  test('renders inline alerts banner when alerts are passed', async () => {
    const alerts = [
      {
        id: 'alert-1',
        project_id: 'proj-1',
        severity: 'ERROR' as const,
        title: 'Zone Capacity Exhausted',
        message: 'Zone us-central1 is exhausted',
        task_id: null,
        created_at: new Date().toISOString(),
        dismissed_at: null,
        acknowledged_at: null,
      },
    ];
    const onDismissAlert = vi.fn();
    const el = await renderPanel({ alerts, onDismissAlert });

    const banner = el.querySelector('[data-testid="alerts-inline-banner"]');
    expect(banner).not.toBeNull();
    expect(banner?.textContent).toContain('Zone Capacity Exhausted');
  });

  test('does not render alerts banner when alerts array is empty', async () => {
    const el = await renderPanel({ alerts: [] });
    expect(el.querySelector('[data-testid="alerts-inline-banner"]')).toBeNull();
  });

  test('does not render alerts banner when alerts prop is not provided', async () => {
    const el = await renderPanel({});
    expect(el.querySelector('[data-testid="alerts-inline-banner"]')).toBeNull();
  });

  test('chat detail toggle reveals the hidden context surfaces and restores the clean transcript', async () => {
    const alerts = [
      {
        id: 'alert-1',
        project_id: 'proj-1',
        severity: 'ERROR' as const,
        title: 'Zone Capacity Exhausted',
        message: 'Zone us-central1 is exhausted',
        task_id: null,
        created_at: new Date().toISOString(),
        dismissed_at: null,
        acknowledged_at: null,
      },
    ];
    const el = await renderPanel({
      filterChrome: 'minimal',
      alerts,
      lastCycleCompletion: {
        status: 'completed',
        created_count: 2,
        tasks_spawned: 1,
        running_update_changed: true,
      },
    });

    // Clean by default: chat hides the operational banners.
    expect(el.querySelector('[data-testid="alerts-inline-banner"]')).toBeNull();
    expect(el.textContent).not.toContain('Cycle complete');

    const detailToggle = el.querySelector('[data-testid="conversation-detail-toggle"]') as HTMLButtonElement | null;
    if (!detailToggle) throw new Error('detail toggle button not found');
    expect(detailToggle.getAttribute('aria-pressed')).toBe('false');

    await act(async () => { detailToggle.click(); });

    expect(detailToggle.getAttribute('aria-pressed')).toBe('true');
    const banner = el.querySelector('[data-testid="alerts-inline-banner"]');
    expect(banner).not.toBeNull();
    expect(banner?.textContent).toContain('Zone Capacity Exhausted');
    expect(el.textContent).toContain('Cycle complete');
    expect(el.textContent).toContain('+2 directions');

    await act(async () => { detailToggle.click(); });

    expect(detailToggle.getAttribute('aria-pressed')).toBe('false');
    expect(el.querySelector('[data-testid="alerts-inline-banner"]')).toBeNull();
    expect(el.textContent).not.toContain('Cycle complete');
  });

  test('loads the next triage page from the reachable load-older control', async () => {
    const newest = Array.from({ length: 30 }, (_, index) => makeMsg({
      id: `newest-${index}`,
      content: `newest message ${index}`,
    }));
    const older = [makeMsg({
      id: 'older-1',
      message_type: 'BLOCKER',
      from_type: 'agent',
      content: 'older retained message',
    })];
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    _setupApiFetchMock({ messagesQueue: [newest, older], messageTotalsQueue: [31, 31] });
    const el = await mountPanel();

    const loadOlder = await waitFor(() => {
      const button = [...el.querySelectorAll('button')]
        .find(candidate => candidate.textContent === 'Load older messages');
      expect(button).toBeDefined();
      return button as HTMLButtonElement;
    });
    await act(async () => { loadOlder.click(); });

    await waitFor(() => {
      const urls = _messagesGetCalls().map(call => String(call[0]));
      expect(urls.some(url => url.includes('offset=30') && url.includes('limit=30'))).toBe(true);
      expect(el.textContent).toContain('older retained message');
      expect([...el.querySelectorAll('button')]
        .some(candidate => candidate.textContent === 'Load older messages')).toBe(false);
    });
  });
  test('uses server total when guaranteed high-signal rows make the response longer than the requested page', async () => {
    const requestedPagePlusGuaranteed = Array.from({ length: 31 }, (_, index) => makeMsg({
      id: `guaranteed-aware-${index}`,
      content: `guaranteed-aware message ${index}`,
    }));
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    _setupApiFetchMock({ defaultMessages: requestedPagePlusGuaranteed, defaultTotal: 90 });
    const el = await mountPanel();

    await waitFor(() => {
      expect([...el.querySelectorAll('button')]
        .some(candidate => candidate.textContent === 'Load older messages')).toBe(true);
    });
  });

  test('keeps a terminal expanded window terminal when the capped newest-window poll runs', async () => {
    const page = (prefix: string, length = 30) => Array.from({ length }, (_, index) => makeMsg({
      id: `${prefix}-${index}`,
      content: `${prefix} message ${index}`,
    }));
    const pages = [
      page('page-0'), page('page-30'), page('page-60'), page('page-90'),
      page('page-120'), page('page-150'), page('page-180-terminal', 25), page('capped-poll'),
    ];
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    _setupApiFetchMock({ messagesQueue: pages, messageTotalsQueue: pages.map(() => 205) });
    const el = await mountPanel();

    for (let pageIndex = 0; pageIndex < 6; pageIndex += 1) {
      const button = await waitFor(() => {
        const candidate = [...el.querySelectorAll('button')]
          .find(item => item.textContent === 'Load older messages');
        expect(candidate).toBeDefined();
        return candidate as HTMLButtonElement;
      });
      await act(async () => { button.click(); });
    }

    await waitFor(() => {
      expect(_messagesGetCalls().map(call => String(call[0]))
        .some(url => url.includes('offset=180') && url.includes('limit=30'))).toBe(true);
      expect([...el.querySelectorAll('button')]
        .some(candidate => candidate.textContent === 'Load older messages')).toBe(false);
    });

    await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
    await waitFor(() => {
      expect(_messagesGetCalls().map(call => String(call[0]))
        .some(url => url.includes('limit=30') && !url.includes('offset='))).toBe(true);
      expect([...el.querySelectorAll('button')]
        .some(candidate => candidate.textContent === 'Load older messages')).toBe(false);
    });
  });

  test('needsActionOnly defaults to true and resets on project switch', async () => {
    // msg-a: in strategicState needs_operator attention items → appears when needsActionOnly=true
    // msg-b: NOT in attention items → hidden when needsActionOnly=true, visible when false
    const msgA = makeMsg({
      id: 'msg-a',
      message_type: 'QUESTION',
      from_type: 'agent',
      content: 'action-required message',
      project_id: 'proj-1',
    });
    const msgB = makeMsg({
      id: 'msg-b',
      message_type: 'QUESTION',
      from_type: 'agent',
      content: 'non-operator message',
      project_id: 'proj-1',
    });

    const proj1State = makeStrategicState({
      project_id: 'proj-1',
      attention: {
        level: 'needs_operator',
        summary: '1 item needs action',
        last_seen_at: null,
        counts: {
          needs_operator: 1,
          watch: 0,
          unread: 0,
          questions: 1,
          blockers: 0,
          approvals: 0,
          manual_directions: 0,
          failed_tasks: 0,
          blocked_tasks: 0,
          parked_tasks: 0,
        },
        items: [
          {
            id: 'message:msg-a',
            kind: 'message' as const,
            severity: 'needs_operator' as const,
            reason: 'question',
            label: 'Question waiting',
            ref: { kind: 'message' as const, id: 'msg-a', project_id: 'proj-1' },
          },
        ],
      },
    });

    // Phase 1: default state — filter bar visible, "Needs action" pressed, msg-b absent
    const el = await renderPanel({ strategicState: proj1State }, [msgA, msgB]);

    const filterBar = el.querySelector('[data-testid="needs-action-filter-bar"]');
    expect(filterBar).not.toBeNull(); // usesStrategicReadModel=true → bar rendered

    const needsActionBtn = el.querySelector<HTMLButtonElement>('[data-testid="needs-action-filter-needs-action"]');
    const showAllBtn = el.querySelector<HTMLButtonElement>('[data-testid="needs-action-filter-show-all"]');
    expect(needsActionBtn).not.toBeNull();
    expect(showAllBtn).not.toBeNull();

    // needsActionOnly defaults to true
    expect(needsActionBtn!.getAttribute('aria-pressed')).toBe('true');
    expect(showAllBtn!.getAttribute('aria-pressed')).toBe('false');

    // Only msg-a (in needsOperatorIds) appears in the message list; msg-b is filtered out
    expect(el.querySelector('[data-message-id="msg-a"]')).not.toBeNull();
    expect(el.querySelector('[data-message-id="msg-b"]')).toBeNull();

    // Phase 2: click "Show all" → needsActionOnly=false → msg-b becomes visible
    await act(async () => { showAllBtn!.click(); });

    expect(needsActionBtn!.getAttribute('aria-pressed')).toBe('false');
    expect(showAllBtn!.getAttribute('aria-pressed')).toBe('true');
    expect(el.querySelector('[data-message-id="msg-b"]')).not.toBeNull();

    // Phase 3: switch to proj-2 → needsActionOnly resets to true
    const proj2State = makeStrategicState({ project_id: 'proj-2' });
    _suppressSinceLastVisit('test-user@example.com', 'proj-2');
    _setupApiFetchMock({ defaultMessages: [] });

    await act(async () => {
      root!.render(
        <MessagesPanel
          selectedProjectId="proj-2"
          addToast={() => {}}
          onMessageCountChange={() => {}}
          strategicState={proj2State}
        />,
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });

    // After project switch, needsActionOnly is reset to true
    const needsActionBtnAfter = el.querySelector<HTMLButtonElement>('[data-testid="needs-action-filter-needs-action"]');
    const showAllBtnAfter = el.querySelector<HTMLButtonElement>('[data-testid="needs-action-filter-show-all"]');
    expect(needsActionBtnAfter!.getAttribute('aria-pressed')).toBe('true');
    expect(showAllBtnAfter!.getAttribute('aria-pressed')).toBe('false');
  });

  test('needs-action accept/dismiss uses void on refreshStrategicState', async () => {
    // The accept/dismiss handlers fire refreshStrategicState fire-and-forget
    // (void'd) after the pending-confirmation API resolves. Exercise both paths
    // through the real handlers so the void'd call sites stay covered.
    const confirmationState = (pendingId: string): StrategicStateResponse => makeStrategicState({
      attention: {
        level: 'needs_operator',
        summary: '1 confirmation needs action',
        last_seen_at: null,
        counts: {
          needs_operator: 1, watch: 0, unread: 0, questions: 0, blockers: 0,
          approvals: 1, manual_directions: 0, failed_tasks: 0, blocked_tasks: 0, parked_tasks: 0,
        },
        items: [
          {
            id: `confirmation:${pendingId}`,
            kind: 'confirmation',
            severity: 'needs_operator',
            reason: 'approval_needed',
            label: 'Confirm deployment plan',
            ref: { kind: 'pending_confirmation', id: pendingId, project_id: 'proj-1' },
          },
        ],
      },
    });

    const findChip = (el: HTMLElement): HTMLButtonElement | undefined =>
      [...(el.querySelector('[data-testid="needs-action-strip"]')?.querySelectorAll('button') ?? [])]
        .find(b => b.textContent?.includes('Confirm deployment plan'));

    // Accept path: the void'd refresh fires after acceptPendingConfirmation resolves.
    const acceptRefresh = vi.fn(async () => {});
    const acceptEl = await renderPanel({
      strategicState: confirmationState('confirm-accept'),
      onStrategicStateRefresh: acceptRefresh,
    });
    const acceptChip = findChip(acceptEl);
    expect(acceptChip).toBeTruthy();
    await act(async () => { acceptChip!.click(); });
    const acceptBtn = acceptEl.querySelector('[data-testid="confirmation-accept-btn"]');
    expect(acceptBtn).toBeTruthy();
    await act(async () => { (acceptBtn as HTMLButtonElement).click(); });
    await waitFor(() => expect(acceptRefresh).toHaveBeenCalledTimes(1));

    // Unmount before the dismiss render so each path gets a fresh panel.
    await act(async () => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;

    // Dismiss path: same handler shape, the void'd refresh fires after dismiss resolves.
    const dismissRefresh = vi.fn(async () => {});
    const dismissEl = await renderPanel({
      strategicState: confirmationState('confirm-dismiss'),
      onStrategicStateRefresh: dismissRefresh,
    });
    const dismissChip = findChip(dismissEl);
    expect(dismissChip).toBeTruthy();
    await act(async () => { dismissChip!.click(); });
    const dismissBtn = dismissEl.querySelector('[data-testid="confirmation-dismiss-btn"]');
    expect(dismissBtn).toBeTruthy();
    await act(async () => { (dismissBtn as HTMLButtonElement).click(); });
    await waitFor(() => expect(dismissRefresh).toHaveBeenCalledTimes(1));
  });

  test('test_triage_poll_request_window_is_constant_after_load_older', async () => {
    // Regression: after load-older, the poll used TRIAGE_PAGE_SIZE + triageOffset
    // (growing to 200) instead of the constant TRIAGE_PAGE_SIZE. Every poll tick
    // should always request exactly TRIAGE_PAGE_SIZE messages from offset=0.
    const page = (prefix: string, length = 30) => Array.from({ length }, (_, index) => makeMsg({
      id: `${prefix}-${index}`,
      content: `${prefix} message ${index}`,
    }));
    const pages = [page('initial'), page('older'), page('poll-after-load-older')];
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    _setupApiFetchMock({ messagesQueue: pages, messageTotalsQueue: [60, 60, 60] });
    const el = await mountPanel();

    const loadOlder = await waitFor(() => {
      const button = [...el.querySelectorAll('button')]
        .find(candidate => candidate.textContent === 'Load older messages');
      expect(button).toBeDefined();
      return button as HTMLButtonElement;
    });
    await act(async () => { loadOlder.click(); });

    await waitFor(() => {
      expect(_messagesGetCalls().map(call => String(call[0]))
        .some(url => url.includes('offset=30') && url.includes('limit=30'))).toBe(true);
    });

    // Trigger a normal poll after load-older; it must use limit=30, not limit=60.
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
    await waitFor(() => {
      const pollUrls = _messagesGetCalls().map(call => String(call[0]));
      const normalPolls = pollUrls.filter(url => !url.includes('offset='));
      expect(normalPolls.every(url => url.includes('limit=30'))).toBe(true);
      expect(normalPolls.some(url => url.includes('limit=60') || url.includes('limit=200'))).toBe(false);
    });
  });
});

describe('MessagesPanel grouping', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => { localStorage.clear(); });

  afterEach(() => {
    const r = root;
    if (r) act(() => r.unmount());
    root = null;
    host?.remove();
    host = null;
    localStorage.clear();
    vi.clearAllMocks();
  });

  async function renderPanel(messages: ReturnType<typeof makeMsg>[]) {
    // Suppress the since-last-visit summary card / divider so legacy
    // grouping-and-section assertions are not polluted by the overlay's text.
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    _mockMessagesFetch(messages);
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <MessagesPanel
          selectedProjectId="proj-1"
          onClose={() => {}}
          addToast={() => {}}
          onMessageCountChange={() => {}}
        />,
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    return host as HTMLDivElement;
  }

  test('routine ops group is collapsed by default', async () => {
    const msgs = [makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' } })];
    const el = await renderPanel(msgs);
    expect(el.textContent).toContain('1 routine ops');
    expect(el.textContent).not.toContain('test content');
  });

  test('routine ops group expands on click', async () => {
    const msgs = [makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' } })];
    const el = await renderPanel(msgs);
    const btn = el.querySelector('button[class*="routine"]') ??
      [...el.querySelectorAll('button')].find(b => b.textContent?.includes('routine ops'));
    expect(btn).toBeTruthy();
    await act(async () => { (btn as HTMLButtonElement).click(); });
    expect(el.textContent).toContain('test content');
  });

  test('high-signal messages render above routine group', async () => {
    const blocker = makeMsg({ id: 'hs1', message_type: 'BLOCKER', from_type: 'agent', content: 'high signal msg' });
    const routine = makeMsg({ id: 'ro1', from_type: 'system', context: { event_type: 'pr_merged' }, content: 'routine msg' });
    const el = await renderPanel([blocker, routine]);
    const text = el.textContent ?? '';
    // high-signal content appears; routine ops is collapsed
    expect(text).toContain('high signal msg');
    expect(text).toContain('1 routine ops');
    expect(text).not.toContain('routine msg');
    // high-signal appears before the routine group in DOM order
    const hsIdx = text.indexOf('high signal msg');
    const grpIdx = text.indexOf('routine ops');
    expect(hsIdx).toBeLessThan(grpIdx);
  });

  test('header shows high-signal badge when high-signal open messages exist', async () => {
    const blocker = makeMsg({ id: 'hs1', message_type: 'BLOCKER', from_type: 'agent' });
    const el = await renderPanel([blocker]);
    expect(el.textContent).toContain('high');
  });

  test('header shows total badge', async () => {
    const routine = makeMsg({ id: 'ro1', from_type: 'system', context: { event_type: 'pr_merged' } });
    const el = await renderPanel([routine]);
    // total badge "1" should appear
    const spans = [...el.querySelectorAll('span')].filter(s => s.textContent?.trim() === '1');
    expect(spans.length).toBeGreaterThan(0);
  });

  test('severity-driven sort: BLOCKER appears before BUG_REPORT in importance mode', async () => {
    const older = new Date(Date.now() - 60_000).toISOString();
    const newer = new Date().toISOString();
    const bug = makeMsg({ id: 'b1', message_type: 'BUG_REPORT', from_type: 'agent', content: 'bug msg', created_at: newer });
    const blocker = makeMsg({ id: 'bl1', message_type: 'BLOCKER', from_type: 'agent', content: 'blocker msg', created_at: older });
    const el = await renderPanel([bug, blocker]);
    const text = el.textContent ?? '';
    // BLOCKER should appear before BUG_REPORT regardless of creation order
    expect(text.indexOf('blocker msg')).toBeLessThan(text.indexOf('bug msg'));
  });

  test('pinned BLOCKER region has visual label when blockers exist alongside other high-signal', async () => {
    const blocker = makeMsg({ id: 'bl1', message_type: 'BLOCKER', from_type: 'agent', content: 'blocker msg' });
    const bug = makeMsg({ id: 'b1', message_type: 'BUG_REPORT', from_type: 'agent', content: 'bug msg' });
    const el = await renderPanel([blocker, bug]);
    expect(el.textContent).toContain('Pinned');
    expect(el.textContent).toContain('Blockers');
  });

  test('no BLOCKER pin label when only blockers exist (no divider needed)', async () => {
    const blocker = makeMsg({ id: 'bl1', message_type: 'BLOCKER', from_type: 'agent', content: 'only blocker' });
    const el = await renderPanel([blocker]);
    // Pin label still appears (there are blockers)
    expect(el.textContent).toContain('Pinned');
  });

  test('auto-promoted failing group renders above routine ops section', async () => {
    // 4 failed auto_fix_result messages — qualifies for promotion (>=3 failed, 100% rate)
    const failedMsgs = Array.from({ length: 4 }, (_, i) =>
      makeMsg({ id: `af${i}`, from_type: 'system', context: { event_type: 'auto_fix_result', result: 'failed' } }),
    );
    const el = await renderPanel(failedMsgs);
    const text = el.textContent ?? '';
    // Promoted group banner should appear
    expect(text).toContain('4 auto-fix results');
    expect(text).toContain('4 failed');
    // The promoted block renders ABOVE the routine ops collapsed section header
    // (the section itself still appears with its summary count even when every
    // routine message is also surfaced in the promoted block above).
    const promotedIdx = text.indexOf('4 auto-fix results');
    const routineIdx = text.indexOf('routine ops');
    expect(promotedIdx).toBeGreaterThanOrEqual(0);
    if (routineIdx !== -1) expect(promotedIdx).toBeLessThan(routineIdx);
  });

  test('routine group with mixed outcomes stays in routine ops section', async () => {
    // 2 failed, 1 merged — below promotion threshold (needs >=3 failed)
    const msgs = [
      makeMsg({ id: 'af1', from_type: 'system', context: { event_type: 'auto_fix_result', result: 'failed' } }),
      makeMsg({ id: 'af2', from_type: 'system', context: { event_type: 'auto_fix_result', result: 'failed' } }),
      makeMsg({ id: 'af3', from_type: 'system', context: { event_type: 'auto_fix_result', result: 'merged' } }),
    ];
    const el = await renderPanel(msgs);
    // Stays inside routine ops group (not promoted into the failing block)
    expect(el.textContent).toContain('routine ops');
    // The promoted/failing red banner is not present — the only "auto-fix results"
    // text comes from the routine section's summary line, not a promoted header.
    expect(el.querySelector('.bg-red-950\\/20')).toBeFalsy();
  });

  test('muted event_type suppresses auto-promotion', async () => {
    const key = 'messages_panel_event_prefs';
    localStorage.setItem(key, JSON.stringify({ auto_fix_result: 'mute' }));
    const failedMsgs = Array.from({ length: 4 }, (_, i) =>
      makeMsg({ id: `af${i}`, from_type: 'system', context: { event_type: 'auto_fix_result', result: 'failed' } }),
    );
    const el = await renderPanel(failedMsgs);
    // Muted messages are hidden entirely — no promotion banner, no routine group
    expect(el.textContent).not.toContain('auto-fix results');
    expect(el.textContent).not.toContain('routine ops');
    localStorage.removeItem(key);
  });

  test('outcome breakdown appears in group label inside routine ops', async () => {
    const msgs = [
      makeMsg({ id: 'af1', from_type: 'system', context: { event_type: 'auto_fix_result', result: 'failed' } }),
      makeMsg({ id: 'af2', from_type: 'system', context: { event_type: 'auto_fix_result', result: 'failed' } }),
      makeMsg({ id: 'af3', from_type: 'system', context: { event_type: 'auto_fix_result', result: 'merged' } }),
    ];
    const el = await renderPanel(msgs);
    // Expand the routine ops group
    const btn = [...el.querySelectorAll('button')].find(b => b.textContent?.includes('routine ops'));
    await act(async () => { (btn as HTMLButtonElement)?.click(); });
    // Group chip label inside expanded section should show breakdown
    expect(el.textContent).toContain('· 2 failed');
  });

  test('sort toggle button is present in header', async () => {
    const el = await renderPanel([makeMsg({ id: 'm1', message_type: 'BLOCKER', from_type: 'agent' })]);
    const btn = [...el.querySelectorAll('button')].find(b => b.textContent?.includes('Importance') || b.textContent?.includes('Recent'));
    expect(btn).toBeTruthy();
  });

  test('switching to Recent mode removes BLOCKER pin label and sorts by time', async () => {
    const older = new Date(Date.now() - 60_000).toISOString();
    const newer = new Date().toISOString();
    const bug = makeMsg({ id: 'b1', message_type: 'BUG_REPORT', from_type: 'agent', content: 'newer bug', created_at: newer });
    const blocker = makeMsg({ id: 'bl1', message_type: 'BLOCKER', from_type: 'agent', content: 'older blocker', created_at: older });
    const el = await renderPanel([bug, blocker]);

    // Click the sort toggle to switch to 'Recent'
    const toggleBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Importance');
    expect(toggleBtn).toBeTruthy();
    await act(async () => { (toggleBtn as HTMLButtonElement).click(); });

    const text = el.textContent ?? '';
    // In Recent mode: newer BUG_REPORT appears before older BLOCKER
    expect(text.indexOf('newer bug')).toBeLessThan(text.indexOf('older blocker'));
    // No pin label in Recent mode
    expect(text).not.toContain('Pinned');
  });
});

/**
 * Live strategist streaming — the last mile of the incremental delivery path.
 *
 * These tests assert the CONVERGENCE contract that makes the preview safe to
 * layer over the authenticated message poll: the preview is purely additive
 * while a turn is in flight, and once the durable reply lands the rendered
 * message set is identical to what the poll alone would have produced.
 */
describe('MessagesPanel live strategist streaming', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
  afterEach(() => {
    const r = root;
    if (r) act(() => r.unmount());
    root = null;
    host?.remove();
    host = null;
    localStorage.clear();
    vi.clearAllMocks();
  });

  function delta(over: Partial<StrategicSessionDelta> & { id: number }): StrategicSessionDelta {
    return { project_id: 'proj-1', session_id: 'sess-1', text: '', ...over };
  }

  const DURABLE_REPLY_AT = '2026-06-01T10:00:10.000Z';
  const STREAM_AT = '2026-06-01T10:00:05.000Z';
  // The durable reply the poll eventually delivers. Its `created_at` is NEWER
  // than every streamed delta's `ts`, which is what retires the preview.
  const durableReply = () => makeMsg({
    id: 'reply-1',
    sequence: 2,
    from_type: 'strategic',
    content: 'The durable strategist reply.',
    surface: 'conversation',
    created_at: DURABLE_REPLY_AT,
  });
  const operatorTurn = () => makeMsg({
    id: 'ask-1',
    sequence: 1,
    from_type: 'user',
    content: 'Operator question',
    surface: 'conversation',
    created_at: '2026-06-01T10:00:00.000Z',
  });

  async function render(props: Partial<MessagesPanelProps>, messages: _MockMsg[]) {
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    _mockMessagesFetch(messages);
    host = document.createElement('div');
    host.style.display = 'flex';
    host.style.flexDirection = 'column';
    host.style.height = '600px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <MessagesPanel
          selectedProjectId="proj-1"
          filterChrome="minimal"
          addToast={() => {}}
          onMessageCountChange={() => {}}
          strategicState={makeStrategicState()}
          {...props}
        />,
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    return host as HTMLDivElement;
  }

  async function rerender(props: Partial<MessagesPanelProps>) {
    const r = root;
    if (!r) throw new Error('not rendered');
    await act(async () => {
      r.render(
        <MessagesPanel
          selectedProjectId="proj-1"
          filterChrome="minimal"
          addToast={() => {}}
          onMessageCountChange={() => {}}
          strategicState={makeStrategicState()}
          {...props}
        />,
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
  }

  function preview(el: HTMLElement) {
    return el.querySelector('[data-testid="strategic-streaming-preview"]');
  }
  /** The rendered durable message set — the thing that must be identical with
   *  and without streaming. Fails loudly if the scroll container is missing so
   *  the convergence assertion can never degrade into comparing '' to ''. */
  function messageListHtml(el: HTMLElement): string {
    const list = el.querySelector('.overflow-y-auto');
    if (!list) throw new Error('conversation scroll container not found');
    const html = list.innerHTML;
    if (!html.includes('The durable strategist reply.')) {
      throw new Error('durable reply not present in the captured message list');
    }
    return html;
  }

  test('no streaming at all: the preview never appears and only durable rows render', async () => {
    const el = await render({}, [operatorTurn(), durableReply()]);

    await waitFor(() => expect(el.textContent).toContain('The durable strategist reply.'));
    expect(preview(el)).toBeNull();
    expect(el.querySelector('[data-testid="strategic-streaming-bubble"]')).toBeNull();
    expect(el.querySelector('[data-testid="strategic-live-activity"]')).toBeNull();
  });

  test('streams then persists: preview shows partial reply, then converges to exactly the poll-only render', async () => {
    // Baseline: what the poll ALONE renders once the reply is durable.
    const baselineEl = await render({}, [operatorTurn(), durableReply()]);
    await waitFor(() => expect(baselineEl.textContent).toContain('The durable strategist reply.'));
    const pollOnlyHtml = messageListHtml(baselineEl);
    act(() => root!.unmount());
    root = null;
    host?.remove();
    host = null;

    // In flight: the reply is streaming and NOT yet in the durable set.
    const transcript: StrategicSessionDelta[] = [
      delta({ id: 1, seq: 1, status: 'responding', ts: STREAM_AT }),
      delta({ id: 2, seq: 2, kind: 'message', text: 'The durable ', ts: STREAM_AT }),
      delta({ id: 3, seq: 3, kind: 'message', text: 'strategist reply.', ts: STREAM_AT }),
    ];
    const el = await render({ strategicSessionTranscript: transcript }, [operatorTurn()]);

    const bubble = el.querySelector('[data-testid="strategic-streaming-bubble"]');
    expect(bubble).toBeTruthy();
    // Contiguous same-kind fragments concatenate with no injected separator.
    expect(el.querySelector('[data-testid="strategic-streaming-reply"]')?.textContent)
      .toBe('The durable strategist reply.');
    // The preview is NOT a durable message row: no id, no selection checkbox,
    // and no message-bubble action affordances.
    expect(bubble?.querySelector('input[type="checkbox"]')).toBeNull();
    expect(bubble?.querySelector('button')).toBeNull();
    expect(bubble?.getAttribute('role')).toBe('status');

    // The poll delivers the durable reply. The stream keeps its buffered
    // deltas (nothing clears them) — convergence must come from reconciliation.
    _mockMessagesFetch([operatorTurn(), durableReply()]);
    await rerender({
      strategicSessionTranscript: [
        ...transcript,
        delta({ id: 4, seq: 4, status: 'done', ts: DURABLE_REPLY_AT }),
      ],
      realtimeRefreshSignal: 1,
    });

    await waitFor(() => expect(el.textContent).toContain('The durable strategist reply.'));
    expect(preview(el)).toBeNull();
    expect(messageListHtml(el)).toBe(pollOnlyHtml);
  });

  test('test_streaming_preview_renders_math_and_citations_like_the_durable_message', async () => {
    const content = 'The **answer** is \\(x^2\\), see d-dec033c2, and landed in #17493.';
    const durable = makeMsg({
      id: 'reply-markdown-1',
      sequence: 2,
      from_type: 'strategic',
      content,
      surface: 'conversation',
      created_at: DURABLE_REPLY_AT,
    });
    const onOpenDirection = vi.fn();
    const githubRepo = 'HenryHamster/BlueprintAIStudio';

    // The durable row establishes the canonical Markdown, math, and citation
    // behavior that the ephemeral preview must match.
    const baselineEl = await render({ onOpenDirection, githubRepo }, [operatorTurn(), durable]);
    await waitFor(() => {
      expect(baselineEl.querySelector('.katex')).toBeTruthy();
      expect([...baselineEl.querySelectorAll('[data-testid="message-bubble"] button')]
        .find(button => button.textContent?.includes('d-dec033c2')))
        .toBeTruthy();
    });
    const durableCitation = [...baselineEl.querySelectorAll('[data-testid="message-bubble"] button')]
      .find(button => button.textContent?.includes('d-dec033c2')) as HTMLButtonElement | undefined;
    expect(durableCitation).toBeTruthy();
    await act(async () => { durableCitation?.click(); });
    expect(onOpenDirection).toHaveBeenCalledWith('dec033c2');
    const durablePr = [...baselineEl.querySelectorAll('[data-testid="message-bubble"] a')]
      .find(anchor => anchor.textContent === '#17493');
    expect(durablePr?.getAttribute('href'))
      .toBe('https://github.com/HenryHamster/BlueprintAIStudio/pull/17493');
    const durableMathHtml = baselineEl.querySelector('.katex')?.innerHTML;
    const durableList = baselineEl.querySelector('.overflow-y-auto');
    expect(durableList).toBeTruthy();
    const pollOnlyHtml = durableList?.innerHTML;

    act(() => root!.unmount());
    root = null;
    host?.remove();
    host = null;
    onOpenDirection.mockClear();

    // The same content arrives in fragments. The preview must run it through
    // the durable renderer while remaining an ephemeral, action-free row.
    const transcript: StrategicSessionDelta[] = [
      delta({ id: 1, seq: 1, status: 'responding', ts: STREAM_AT }),
      delta({ id: 2, seq: 2, kind: 'reasoning', text: 'Reasoning stays **plain** \\(y^2\\) and mentions d-dec033c2 #17493.', ts: STREAM_AT }),
      delta({ id: 3, seq: 3, kind: 'message', text: 'The **answer** is \\(x^2\\), see d-dec033c2, and ', ts: STREAM_AT }),
      delta({ id: 4, seq: 4, kind: 'message', text: 'landed in #17493.', ts: STREAM_AT }),
    ];
    const el = await render({ strategicSessionTranscript: transcript, onOpenDirection, githubRepo }, [operatorTurn()]);
    const reply = el.querySelector('[data-testid="strategic-streaming-reply"]');
    const reasoning = el.querySelector('[data-testid="strategic-streaming-reasoning"]');
    await waitFor(() => {
      expect(reply?.querySelector('.katex')).toBeTruthy();
      expect(reply?.querySelector('strong')?.textContent).toBe('answer');
      expect(reply?.querySelector('button')?.textContent).toContain('d-dec033c2');
      expect(reply?.querySelector('a')?.textContent).toBe('#17493');
    });
    expect(reply?.querySelector('.katex')?.innerHTML)
      .toBe(durableMathHtml);
    expect(reply?.querySelector('a')?.getAttribute('href'))
      .toBe(durablePr?.getAttribute('href'));
    expect(reasoning?.textContent)
      .toBe('Reasoning stays **plain** \\(y^2\\) and mentions d-dec033c2 #17493.');
    expect(reasoning?.className).toContain('whitespace-pre-wrap');
    expect(reasoning?.querySelector('.katex')).toBeNull();
    expect(reasoning?.querySelector('button, a, strong')).toBeNull();
    const previewCitation = reply?.querySelector('button') as HTMLButtonElement | null;
    expect(previewCitation).toBeTruthy();
    await act(async () => { previewCitation?.click(); });
    expect(onOpenDirection).toHaveBeenCalledWith('dec033c2');
    expect(el.querySelector('[data-testid="strategic-streaming-bubble"] input[type="checkbox"]')).toBeNull();

    // Once the canonical row arrives, the ephemeral preview retires and the
    // durable message list is byte-for-byte the poll-only baseline.
    _mockMessagesFetch([operatorTurn(), durable]);
    await rerender({
      strategicSessionTranscript: [
        ...transcript,
        delta({ id: 5, seq: 5, status: 'done', ts: DURABLE_REPLY_AT }),
      ],
      onOpenDirection,
      githubRepo,
      realtimeRefreshSignal: 1,
    });
    await waitFor(() => expect(el.textContent).toContain('#17493'));
    expect(preview(el)).toBeNull();
    expect(el.querySelector('.overflow-y-auto')?.innerHTML).toBe(pollOnlyHtml);
  });

  test('stream dropped mid-reply: no done frame ever arrives, the poll alone converges the thread', async () => {
    // A truncated stream: reply fragments, no `done`, and the transport dies.
    const transcript: StrategicSessionDelta[] = [
      delta({ id: 1, seq: 1, status: 'responding', ts: STREAM_AT }),
      delta({ id: 2, seq: 2, kind: 'message', text: 'The durable str', ts: STREAM_AT }),
    ];
    const el = await render({ strategicSessionTranscript: transcript }, [operatorTurn()]);
    expect(el.querySelector('[data-testid="strategic-streaming-reply"]')?.textContent)
      .toBe('The durable str');

    // Only the poll delivers the reply; the stale partial deltas stay buffered.
    _mockMessagesFetch([operatorTurn(), durableReply()]);
    await rerender({ strategicSessionTranscript: transcript, realtimeRefreshSignal: 1 });

    await waitFor(() => expect(el.textContent).toContain('The durable strategist reply.'));
    // The watermark retires the truncated preview even with no `done` frame.
    expect(preview(el)).toBeNull();
    expect(el.textContent).not.toContain('The durable str.');
  });

  test('reasoning segments render set apart from reply segments', async () => {
    const el = await render({
      strategicSessionTranscript: [
        delta({ id: 1, seq: 1, kind: 'reasoning', text: 'Checking the release gate first.' }),
        delta({ id: 2, seq: 2, kind: 'message', text: 'Here is the answer.' }),
      ],
    }, [operatorTurn()]);

    const reasoning = el.querySelector('[data-testid="strategic-streaming-reasoning"]');
    const reply = el.querySelector('[data-testid="strategic-streaming-reply"]');
    expect(reasoning?.textContent).toBe('Checking the release gate first.');
    expect(reply?.textContent).toBe('Here is the answer.');
    // Set apart, not merged into one block, and visually distinct.
    expect(reasoning).not.toBe(reply);
    expect(reasoning?.className).toContain('italic');
    expect(reply?.className).not.toContain('italic');
  });

  test('activity strip renders in-flight tool activity and marks the running item', async () => {
    const el = await render({
      strategicSessionTranscript: [
        delta({ id: 1, seq: 1, status: 'responding' }),
        delta({ id: 2, seq: 2, kind: 'read', text: 'read release_scoring.py' }),
        delta({ id: 3, seq: 3, kind: 'command', text: 'pytest testing/test_release_scoring.py' }),
      ],
    }, [operatorTurn()]);

    const items = [...el.querySelectorAll('[data-testid="strategic-live-activity-item"]')];
    expect(items.map(i => i.textContent)).toEqual([
      'readread release_scoring.py',
      'runpytest testing/test_release_scoring.py',
    ]);
    // Only the LAST item of an in-flight turn is marked as still running.
    expect(items[0].getAttribute('data-in-flight')).toBeNull();
    expect(items[1].getAttribute('data-in-flight')).toBe('true');
  });

  test('deltas stamped for another project never render under this project', async () => {
    const el = await render({
      strategicSessionTranscript: [
        delta({ id: 1, seq: 1, project_id: 'proj-2', kind: 'message', text: 'Other project secret' }),
        delta({ id: 2, seq: 2, project_id: undefined, kind: 'message', text: 'Unattributed delta' }),
      ],
    }, [operatorTurn()]);

    expect(preview(el)).toBeNull();
    expect(el.textContent).not.toContain('Other project secret');
    expect(el.textContent).not.toContain('Unattributed delta');
  });

  test('chat_mode_live_turn_shows_activity_and_age_without_resident', async () => {
    const transcript: StrategicSessionDelta[] = [
      delta({ id: 1, seq: 1, status: 'responding', ts: STREAM_AT }),
      delta({ id: 2, seq: 2, kind: 'command', text: 'grep release gate', ts: STREAM_AT }),
      delta({ id: 3, seq: 3, kind: 'message', text: 'Streaming answer', ts: STREAM_AT }),
    ];
    const el = await render({ strategicSessionTranscript: transcript }, [operatorTurn()]);

    const liveStatus = el.querySelector('[data-testid="conversation-workday-status"]');
    expect(liveStatus).toHaveTextContent('Replying');
    expect(liveStatus).toHaveTextContent('grep release gate');
    expect(liveStatus?.querySelector('[data-testid="conversation-chat-age"]')?.textContent).toMatch(/ago$/);
    expect(liveStatus).toHaveAttribute('data-chat-live-phase', 'streaming');
    expect(liveStatus).not.toHaveTextContent('Workday off');
    expect(el.querySelector('[data-testid="strategic-streaming-reply"]')?.textContent)
      .toBe('Streaming answer');

    _mockMessagesFetch([operatorTurn(), durableReply()]);
    await rerender({
      strategicSessionTranscript: [
        ...transcript,
        delta({ id: 4, seq: 4, status: 'done', ts: DURABLE_REPLY_AT }),
      ],
      realtimeRefreshSignal: 1,
    });

    await waitFor(() => expect(el.textContent).toContain('The durable strategist reply.'));
    const settledStatus = el.querySelector('[data-testid="conversation-workday-status"]');
    expect(settledStatus).toHaveTextContent('Workday off');
    expect(settledStatus).not.toHaveAttribute('data-chat-live-phase');
  });
});
