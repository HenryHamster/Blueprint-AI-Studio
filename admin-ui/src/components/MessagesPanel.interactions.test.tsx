import { describe, test, expect, vi, afterEach, beforeEach } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { render, waitFor } from '@testing-library/react';
import { MessagesPanel, type MessagesPanelProps } from './MessagesPanel';
import type { ProjectMessage } from './MessageBubble';
import { loadComposerDraft } from '../utils/conversationDraftState';
import type { StrategicStateResponse } from '../strategicStateApi';

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
vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>();
  return {
    ...actual,
    apiFetch: vi.fn(async () => new Response(JSON.stringify({ messages: [] }), { status: 200 })),
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

type _MockMsg = ReturnType<typeof makeMsg>;
function _setupApiFetchMock(opts: {
  messagesQueue?: _MockMsg[][];
  defaultMessages?: _MockMsg[];
  lastSeenAt?: number | null;
}): void {
  const queue = opts.messagesQueue;
  const defaultMessages = opts.defaultMessages ?? [];
  const lastSeenAt = opts.lastSeenAt === undefined ? null : opts.lastSeenAt;
  let qIdx = 0;
  (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
    const urlStr = String(url);
    const initObj = init as RequestInit | undefined;
    const method = initObj?.method ?? 'GET';
    if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
      return new Response(JSON.stringify({ last_seen_at: lastSeenAt }), { status: 200 });
    }
    if (/\/api\/admin\/projects\/[^/]+\/strategic-conversation(?:\?|$)/.test(urlStr) && method === 'GET') {
      const msgs = queue
        ? queue[Math.min(qIdx, queue.length - 1)]
        : defaultMessages;
      if (queue) qIdx += 1;
      return new Response(JSON.stringify({
        conversation: {
          limit: 20,
          has_more: false,
          order: 'oldest_first',
          items: msgs,
        },
      }), { status: 200 });
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
    if (/\/api\/admin\/messages(\?|$)/.test(urlStr) && method === 'GET') {
      const msgs = queue
        ? queue[Math.min(qIdx, queue.length - 1)]
        : defaultMessages;
      if (queue) qIdx += 1;
      return new Response(JSON.stringify({ messages: msgs }), { status: 200 });
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
function _messagesGetCalls(): unknown[][] {
  return vi.mocked(apiFetch).mock.calls.filter((c) => {
    const url = String(c[0]);
    const init = c[1] as RequestInit | undefined;
    return /\/api\/admin\/messages(\?|$)/.test(url) && (!init?.method || init.method === 'GET');
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

// ── groupRoutineMessages unit tests ──────────────────────────────────────────

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


// ── Auto-scroll behavior ───────────────────────────────────────────────────────
// Verifies the two scroll-to-bottom cases that the operator complained were
// unreliable: (1) operator's own just-sent message always scrolls, (2) incoming
// messages scroll when near bottom but not when far away.

describe('MessagesPanel auto-scroll', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });

  afterEach(() => {
    const r = root;
    if (r) act(() => r.unmount());
    root = null;
    host?.remove();
    host = null;
    sessionStorage.clear();
    vi.clearAllMocks();
  });

  function _setupStrategicSendMock(): void {
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (/\/api\/admin\/projects\/[^/]+\/strategic-session\/messages$/.test(urlStr) && method === 'POST') {
        const payload = JSON.parse(String((init as RequestInit | undefined)?.body ?? '{}')) as {
          content?: string;
          idempotency_key?: string;
          message_type?: ProjectMessage['message_type'];
        };
        return new Response(JSON.stringify({
          arming: 'steering',
          wakeup_state: 'queued',
          message: makeMsg({
            id: `operator-${payload.idempotency_key ?? 'send'}`,
            content: payload.content ?? '',
            message_type: payload.message_type ?? 'FEEDBACK',
            from_type: 'user',
            actor_user_id: 'test-user@example.com',
            surface: 'conversation',
            handling_state: 'pending',
            client_idempotency_key: payload.idempotency_key ?? null,
            idempotency_key: payload.idempotency_key ?? null,
          }),
        }), { status: 200 });
      }
      if (/\/api\/admin\/projects\/[^/]+\/strategic-conversation(?:\?|$)/.test(urlStr) && method === 'GET') {
        return new Response(JSON.stringify({
          conversation: { limit: 20, has_more: false, order: 'oldest_first', items: [] },
        }), { status: 200 });
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
  }

  async function renderConversationPanel(): Promise<HTMLDivElement> {
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    _setupStrategicSendMock();
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
          filterChrome="minimal"
        />,
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    return host as HTMLDivElement;
  }

  test('operator own message always scrolls to bottom regardless of scroll position', async () => {
    const scrollTo = vi.fn();
    const originalScrollTo = HTMLElement.prototype.scrollTo;
    HTMLElement.prototype.scrollTo = scrollTo;

    try {
      const el = await renderConversationPanel();

      // Simulate user being far from the bottom (scrollHeight >> scrollTop + clientHeight).
      const scrollContainer = el.querySelector('[class*="overflow-y-auto"]') as HTMLElement;
      expect(scrollContainer).toBeTruthy();
      Object.defineProperty(scrollContainer, 'scrollHeight', { configurable: true, get: () => 2000 });
      Object.defineProperty(scrollContainer, 'scrollTop', { configurable: true, get: () => 0 });
      Object.defineProperty(scrollContainer, 'clientHeight', { configurable: true, get: () => 400 });

      scrollTo.mockClear();

      // Type and send a message in conversation mode
      const textarea = el.querySelector('textarea') as HTMLTextAreaElement;
      expect(textarea).toBeTruthy();
      const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')!.set!;
      await act(async () => {
        nativeSetter.call(textarea, 'My new message');
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
      });
      const sendBtn = el.querySelector('button[aria-label="Send message"]') as HTMLButtonElement;
      expect(sendBtn).toBeTruthy();
      await act(async () => { sendBtn.click(); });
      await act(async () => { await new Promise<void>(res => setTimeout(res, 0)); });

      // scrollTo must have been called (optimistic send always scrolls)
      expect(scrollTo).toHaveBeenCalled();
      const lastCall = scrollTo.mock.calls.at(-1);
      expect(lastCall?.[0]).toMatchObject({ top: expect.any(Number) });
    } finally {
      HTMLElement.prototype.scrollTo = originalScrollTo;
    }
  });

  test('incoming message does not scroll when user is far from bottom', async () => {
    let messageQueue = [[] as ReturnType<typeof makeMsg>[], [] as ReturnType<typeof makeMsg>[]];
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (/\/api\/admin\/projects\/[^/]+\/strategic-conversation(?:\?|$)/.test(urlStr) && method === 'GET') {
        const batch = messageQueue.shift() ?? [];
        return new Response(JSON.stringify({
          conversation: { limit: 20, has_more: false, order: 'oldest_first', items: batch },
        }), { status: 200 });
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
    // Second fetch returns one new agent message
    messageQueue = [
      [],
      [makeMsg({ id: 'agent-reply', from_type: 'agent', message_type: 'NOTE', content: 'Agent reply' })],
    ];

    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <MessagesPanel selectedProjectId="proj-1" onClose={() => {}} addToast={() => {}} onMessageCountChange={() => {}} filterChrome="minimal" />,
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    const el = host as HTMLDivElement;

    const scrollContainer = el.querySelector('[class*="overflow-y-auto"]') as HTMLElement;
    expect(scrollContainer).toBeTruthy();
    // Far from bottom: gap = 2000 - 0 - 400 = 1600 px >> 200 px threshold
    Object.defineProperty(scrollContainer, 'scrollHeight', { configurable: true, get: () => 2000 });
    Object.defineProperty(scrollContainer, 'scrollTop', { configurable: true, get: () => 0 });
    Object.defineProperty(scrollContainer, 'clientHeight', { configurable: true, get: () => 400 });

    const scrollTo = vi.fn();
    const originalScrollTo = HTMLElement.prototype.scrollTo;
    HTMLElement.prototype.scrollTo = scrollTo;

    try {
      scrollTo.mockClear();
      // Trigger a re-render that delivers the new agent message (simulate polling)
      await act(async () => {
        r.render(
          <MessagesPanel
            selectedProjectId="proj-1"
            onClose={() => {}}
            addToast={() => {}}
            onMessageCountChange={() => {}}
            filterChrome="minimal"
            realtimeRefreshSignal={Date.now()}
          />,
        );
      });
      await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });

      // No scroll — user was far from bottom
      expect(scrollTo).not.toHaveBeenCalled();
    } finally {
      HTMLElement.prototype.scrollTo = originalScrollTo;
    }
  });
});

// ── Feedback → direction conversion idempotency ───────────────────────────────
// The operator can convert a FEEDBACK message into a steering direction. A
// retried (or batch-replayed) conversion of the SAME message must reuse one
// client idempotency identity so the backend collapses it to a single
// direction, and a backend duplicate replay (`status: "duplicate"`) is a clean
// success — not an error. (The POST /directions backend dedup is covered in
// test_directions.py; these tests lock the client key reuse + replay handling.)

describe('MessagesPanel handleCreateDirection idempotency', () => {
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

  // Installs an apiFetch mock that serves a single FEEDBACK message on the list
  // GET and records every POST /directions body. `failFirstPost` makes the first
  // create POST fail (500) so the retry path can be exercised; `duplicate` makes
  // the create response carry `status: "duplicate"`.
  function _setupDirectionMock(opts: { failFirstPost?: boolean; failFirstPatch?: boolean; duplicate?: boolean } = {}): {
    postBodies: Record<string, unknown>[];
    patchBodies: Record<string, unknown>[];
  } {
    const postBodies: Record<string, unknown>[] = [];
    const patchBodies: Record<string, unknown>[] = [];
    let postCount = 0;
    let patchCount = 0;
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/directions') && method === 'POST') {
        postBodies.push(JSON.parse(String((init as RequestInit).body)));
        postCount += 1;
        if (opts.failFirstPost && postCount === 1) return new Response('boom', { status: 500 });
        return new Response(
          JSON.stringify({ direction: { id: 'd-xyz' }, ...(opts.duplicate ? { status: 'duplicate' } : {}) }),
          { status: 200 },
        );
      }
      if (urlStr.includes('/api/admin/messages/fb-1') && method === 'PATCH') {
        patchBodies.push(JSON.parse(String((init as RequestInit).body)));
        patchCount += 1;
        if (opts.failFirstPatch && patchCount === 1) return new Response('boom', { status: 500 });
        return new Response(JSON.stringify({}), { status: 200 });
      }
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (/\/api\/admin\/messages(\?|$)/.test(urlStr) && method === 'GET') {
        return new Response(JSON.stringify({ messages: [makeMsg({ id: 'fb-1', message_type: 'FEEDBACK', from_type: 'operator' })] }), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });
    return { postBodies, patchBodies };
  }

  async function renderPanel(addToast: (message: string, type: 'success' | 'error' | 'info') => void = () => {}) {
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <MessagesPanel
          selectedProjectId="proj-1"
          onClose={() => {}}
          addToast={addToast}
          onMessageCountChange={() => {}}
        />,
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    return host as HTMLDivElement;
  }

  // Returns the button that opens the convert-to-direction modal.
  function createDirectionButton(el: HTMLElement): HTMLButtonElement {
    const btn = el.querySelector('button[title="Create Direction"]') as HTMLButtonElement | null;
    if (!btn) throw new Error('test setup: expected a Create Direction button on the feedback message');
    return btn;
  }

  // Opens the convert-to-direction modal by clicking the icon button, then
  // returns the modal's "Create direction" submit button.
  async function openModalAndGetSubmitButton(el: HTMLElement): Promise<HTMLButtonElement> {
    const iconBtn = createDirectionButton(el);
    await act(async () => { iconBtn.click(); });
    await act(async () => {});
    const submitBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Create direction') as HTMLButtonElement | undefined;
    if (!submitBtn) throw new Error('test setup: expected a "Create direction" submit button in the modal');
    return submitBtn;
  }

  test('sends an idempotency_key with the direction create POST', async () => {
    const { postBodies } = _setupDirectionMock();
    const el = await renderPanel();
    const submitBtn = await openModalAndGetSubmitButton(el);
    await act(async () => { submitBtn.click(); });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 0)); });

    expect(postBodies.length).toBe(1);
    expect(typeof postBodies[0].idempotency_key).toBe('string');
    expect((postBodies[0].idempotency_key as string).length).toBeGreaterThan(0);
  });

  test('reuses one idempotency_key per message id when a failed conversion is retried', async () => {
    const { postBodies } = _setupDirectionMock({ failFirstPost: true });
    const el = await renderPanel();
    // Open modal; first submit fails (POST 500) → modal stays open
    const submitBtn = await openModalAndGetSubmitButton(el);
    await act(async () => { submitBtn.click(); }); // first attempt fails — key retained
    await act(async () => { await new Promise<void>(res => setTimeout(res, 0)); });
    // Modal stays open after failure — submit again with the same key
    await act(async () => { submitBtn.click(); }); // retry — same message id, same key
    await act(async () => { await new Promise<void>(res => setTimeout(res, 0)); });

    expect(postBodies.length).toBe(2);
    expect(postBodies[0].idempotency_key).toBeTruthy();
    expect(postBodies[0].idempotency_key).toBe(postBodies[1].idempotency_key);
  });

  test('keeps one idempotency_key until the resolution PATCH succeeds', async () => {
    const addToast = vi.fn();
    const { postBodies, patchBodies } = _setupDirectionMock({ failFirstPatch: true, duplicate: true });
    const el = await renderPanel(addToast);
    // Open modal; first submit: POST succeeds but PATCH fails → modal stays open
    const submitBtn = await openModalAndGetSubmitButton(el);
    await act(async () => { submitBtn.click(); }); // create succeeds, resolution PATCH fails
    await act(async () => { await new Promise<void>(res => setTimeout(res, 0)); });
    // Modal stays open; retry replays same idempotency key
    await act(async () => { submitBtn.click(); }); // retry replays the same create idempotency key
    await act(async () => { await new Promise<void>(res => setTimeout(res, 0)); });

    expect(postBodies.length).toBe(2);
    expect(postBodies[0].idempotency_key).toBeTruthy();
    expect(postBodies[0].idempotency_key).toBe(postBodies[1].idempotency_key);
    expect(patchBodies).toEqual([
      { resolution: 'Direction created: d-xyz' },
      { resolution: 'Direction created: d-xyz' },
    ]);
    expect(addToast).toHaveBeenCalledWith('Failed to create direction', 'error');
    expect(addToast).toHaveBeenCalledWith('Direction created', 'success');
  });

  test('treats a status:"duplicate" create response as a clean success', async () => {
    const { postBodies } = _setupDirectionMock({ duplicate: true });
    const el = await renderPanel();
    const submitBtn = await openModalAndGetSubmitButton(el);
    await act(async () => { submitBtn.click(); });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 0)); });

    // One create, and the message was resolved (PATCH) — the duplicate replay
    // routed through the success path rather than throwing.
    expect(postBodies.length).toBe(1);
    const patched = vi.mocked(apiFetch).mock.calls.some((c) => {
      const init = c[1] as RequestInit | undefined;
      return String(c[0]).includes('/api/admin/messages/fb-1') && init?.method === 'PATCH';
    });
    expect(patched).toBe(true);
  });
});


describe('MessagesPanel strategic-refresh pending badge', () => {
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

  // Installs a mock where GET /api/admin/projects/{id} returns the supplied vision,
  // and the messages list is empty. Mirrors _setupApiFetchMock's URL routing.
  function _mockWithVision(vision: Record<string, unknown> | null): void {
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: Date.now() + 1e9 }), { status: 200 });
      }
      if (/\/api\/admin\/messages(\?|$)/.test(urlStr) && method === 'GET') {
        return new Response(JSON.stringify({ messages: [] }), { status: 200 });
      }
      // Project detail GET carries the vision marker; tolerate trailing query.
      if (/\/api\/admin\/projects\/[^/]+($|\?)/.test(urlStr) && method === 'GET') {
        return new Response(JSON.stringify({ vision: vision ?? {} }), { status: 200 });
      }
      if (method === 'GET' && (
        urlStr.includes('/noise-prefs') || urlStr.includes('/saved-views') ||
        urlStr.includes('/temp-mutes') || urlStr.includes('/mute-window')
      )) {
        return new Response('', { status: 404 });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });
  }

  async function renderPanel(
    vision: Record<string, unknown> | null,
    strategistActive = false,
    props: Partial<MessagesPanelProps> = {},
  ) {
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    _mockWithVision(vision);
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
          strategistActive={strategistActive}
          {...props}
        />,
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    return host as HTMLDivElement;
  }

  test('shows the compact pending receipt when a marker is present', async () => {
    const el = await renderPanel({
      _strategic_refresh: { requested_at: new Date(Date.now() - 60_000).toISOString(), origin: 'steering', reason: 'op', count: 1 },
    });
    expect(el.textContent).toContain('Strategic cycle pending');
    expect(el.textContent).toContain('Cancel');
    expect(el.textContent).not.toContain('steering');
  });

  test('hides pending badge when no marker is present', async () => {
    const el = await renderPanel({});
    expect(el.textContent).not.toContain('Strategic cycle pending');
  });

  test('falls back to legacy project vision while keeping the pending receipt compact', async () => {
    const el = await renderPanel(
      {
        _strategic_refresh: {
          requested_at: new Date(Date.now() - 60_000).toISOString(),
          origin: 'operator',
        },
      },
      false,
      { strategicState: null },
    );

    expect(el.textContent).toContain('Strategic cycle pending');
    expect(el.textContent).toContain('Cancel');
    expect(el.textContent).not.toContain('operator');
  });

  test('shows a compact pending receipt from the populated Strategic read model', async () => {
    const state = makeStrategicState({
      strategic: {
        ...makeStrategicState().strategic,
        refresh: {
          pending: {
            requested_at: new Date(Date.now() - 60_000).toISOString(),
            origin: 'operator',
            excerpt: 'Refresh from Strategic read model',
            operator_count: 2,
          },
          last_fired_at: null,
          last_fired_origin: null,
        },
      },
    });
    const el = await renderPanel({}, false, { strategicState: state });

    expect(el.textContent).toContain('Strategic cycle pending');
    expect(el.textContent).toContain('Cancel');
    expect(el.textContent).not.toContain('Refresh from Strategic read model');
    expect(el.textContent).not.toContain('coalesced');
  });

  test('hides pending badge once the cycle is running (strategistActive)', async () => {
    const el = await renderPanel(
      { _strategic_refresh: { requested_at: new Date().toISOString(), origin: 'steering' } },
      true,
    );
    expect(el.textContent).not.toContain('Strategic cycle pending');
    expect(el.textContent).toContain('Strategist working');
  });

  test('Cancel button posts to cancel-strategic-refresh and clears the badge', async () => {
    const el = await renderPanel({
      _strategic_refresh: { requested_at: new Date(Date.now() - 60_000).toISOString(), origin: 'steering' },
    });
    const cancelBtn = Array.from(el.querySelectorAll('button')).find(b => b.textContent === 'Cancel');
    expect(cancelBtn).toBeTruthy();

    // After cancel, the project GET returns no marker so the badge clears.
    _mockWithVision({});
    await act(async () => { cancelBtn!.click(); });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });

    const mock = apiFetch as ReturnType<typeof vi.fn>;
    const calledCancel = mock.mock.calls.some(([url, init]) =>
      String(url).includes('/cancel-strategic-refresh') && (init as RequestInit | undefined)?.method === 'POST');
    expect(calledCancel).toBe(true);
    expect(el.textContent).not.toContain('Strategic cycle pending');
  });

  test('no Cancel button on the receipt (non-pending) state', async () => {
    const el = await renderPanel({
      _strategic_refresh_last_fired_at: new Date(Date.now() - 60_000).toISOString(),
      _strategic_refresh_last_fired_origin: 'operator',
    });
    expect(el.textContent).toContain('Steer picked up');
    const cancelBtn = Array.from(el.querySelectorAll('button')).find(b => b.textContent === 'Cancel');
    expect(cancelBtn).toBeFalsy();
  });

  test('does not leak a pending directive into the compact conversation chrome', async () => {
    const el = await renderPanel({
      _strategic_refresh: {
        requested_at: new Date(Date.now() - 60_000).toISOString(),
        excerpt: 'Fix the flaky test in the auth module by adding proper setup/teardown',
        origin: 'operator',
        count: 1,
      },
    });
    expect(el.textContent).toContain('Strategic cycle pending');
    expect(el.textContent).not.toContain('Fix the flaky test in the auth module by adding proper setup/teardown');
  });

  test('keeps long pending directives out of the compact conversation chrome', async () => {
    const longExcerpt = 'This is a very long directive text that should be truncated with ellipsis because it exceeds the maximum display length and we want to keep the UI compact and readable for the operator'.repeat(2);
    const el = await renderPanel({
      _strategic_refresh: {
        requested_at: new Date(Date.now() - 60_000).toISOString(),
        excerpt: longExcerpt,
        origin: 'operator',
        count: 1,
      },
    });
    expect(el.textContent).toContain('Strategic cycle pending');
    expect(el.textContent).not.toContain(longExcerpt.slice(0, 120));
  });

  test('coalesced steers keep a single compact pending receipt', async () => {
    const el = await renderPanel({
      _strategic_refresh: {
        requested_at: new Date(Date.now() - 60_000).toISOString(),
        excerpt: 'Update the problem model for the new version',
        origin: 'operator',
        count: 3,
        operator_count: 3,
      },
    });
    expect(el.textContent).toContain('Strategic cycle pending');
    expect(el.textContent).not.toContain('coalesced');
    const cancelButtons = Array.from(el.querySelectorAll('button'))
      .filter(button => button.textContent === 'Cancel');
    expect(cancelButtons).toHaveLength(1);
  });

  test('does not display coalesce count for qualification-origin markers (automated refreshes excluded)', async () => {
    // A qualification marker with count > 1 should NOT show coalesce since the label
    // uses operator_count, which is 0 for automated refreshes.
    const el = await renderPanel({
      _strategic_refresh: {
        requested_at: new Date(Date.now() - 60_000).toISOString(),
        excerpt: 'Update the problem model for the new version',
        origin: 'qualification',
        count: 3,
        operator_count: 0,
      },
    });
    expect(el.textContent).toContain('Strategic cycle pending');
    expect(el.textContent).not.toContain('coalesced');
  });

  test('does not display coalesce info when operator_count = 1', async () => {
    const el = await renderPanel({
      _strategic_refresh: {
        requested_at: new Date(Date.now() - 60_000).toISOString(),
        excerpt: 'Update the problem model for the new version',
        origin: 'operator',
        count: 1,
        operator_count: 1,
      },
    });
    expect(el.textContent).toContain('Strategic cycle pending');
    expect(el.textContent).not.toContain('coalesced');
  });

  test('multiple pending steers remain one compact receipt without verbose details', async () => {
    const el = await renderPanel({
      _strategic_refresh: {
        requested_at: new Date(Date.now() - 120_000).toISOString(),
        excerpt: 'Investigate the performance regression in the query handler',
        origin: 'operator',
        count: 5,
        operator_count: 5,
      },
    });
    expect(el.textContent).toContain('Strategic cycle pending');
    expect(el.textContent).not.toContain('Investigate the performance regression in the query handler');
    expect(el.textContent).not.toContain('coalesced');
    expect(el.textContent).not.toContain('armed 2m ago');
  });

  test('does not show receipt when last_fired_at is older than 2 hours', async () => {
    // A stale receipt (> 2h old) must be suppressed — it grows without bound otherwise
    // and resurfaces confusingly after an operator cancels a fresh steer.
    const staleAt = new Date(Date.now() - 3 * 3600_000).toISOString(); // 3 hours ago
    const el = await renderPanel({
      _strategic_refresh_last_fired_at: staleAt,
      _strategic_refresh_last_fired_origin: 'operator',
    });
    expect(el.textContent).not.toContain('Steer picked up');
  });

  test('shows receipt when last_fired_at is within 2 hours', async () => {
    // A recent receipt (< 2h old) must show — the steer just fired and context is fresh.
    const recentAt = new Date(Date.now() - 30 * 60_000).toISOString(); // 30 minutes ago
    const el = await renderPanel({
      _strategic_refresh_last_fired_at: recentAt,
      _strategic_refresh_last_fired_origin: 'operator',
    });
    expect(el.textContent).toContain('Steer picked up');
  });

  test('shows receipt for a naive-UTC (no trailing Z) timestamp within 2 hours', async () => {
    // The backend serialises naive UTC ISO strings *without* a trailing 'Z'.
    // The age gate must parse these through parseTimestamp (UTC), not raw
    // new Date() (which would treat them as local time and shift the age by
    // the operator's UTC offset).
    const recentNaive = new Date(Date.now() - 30 * 60_000).toISOString().replace('Z', ''); // 30 min ago, naive UTC
    const el = await renderPanel({
      _strategic_refresh_last_fired_at: recentNaive,
      _strategic_refresh_last_fired_origin: 'operator',
    });
    expect(el.textContent).toContain('Steer picked up');
  });

  test('hides receipt for a naive-UTC (no trailing Z) timestamp older than 2 hours', async () => {
    // Complementary stale case: a 3h-old naive-UTC timestamp must be suppressed.
    // Proves the gate is parsed as UTC, not local — a stale receipt stays hidden.
    const staleNaive = new Date(Date.now() - 3 * 3600_000).toISOString().replace('Z', ''); // 3h ago, naive UTC
    const el = await renderPanel({
      _strategic_refresh_last_fired_at: staleNaive,
      _strategic_refresh_last_fired_origin: 'operator',
    });
    expect(el.textContent).not.toContain('Steer picked up');
  });
});

// ── Composer draft persistence tests ─────────────────────────────────────────

describe('MessagesPanel composer draft persistence', () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
  afterEach(() => { localStorage.clear(); sessionStorage.clear(); vi.clearAllMocks(); });

  function renderDraftPanel(projectId = 'proj-draft') {
    _suppressSinceLastVisit('test-user@example.com', projectId);
    _mockMessagesFetch([]);
    return render(
      <MessagesPanel
        selectedProjectId={projectId}
        onClose={() => {}}
        addToast={() => {}}
        onMessageCountChange={() => {}}
      />,
    );
  }

  test('draft persists in localStorage after typing into the composer', async () => {
    const { container } = renderDraftPanel();
    await waitFor(() => expect(_messagesGetCalls().length).toBeGreaterThanOrEqual(1));

    const textarea = container.querySelector('textarea') as HTMLTextAreaElement;
    expect(textarea).toBeTruthy();

    const { fireEvent: fe } = await import('@testing-library/react');
    await act(async () => {
      fe.change(textarea, { target: { value: 'draft text to persist' } });
    });

    const parsed = loadComposerDraft('test-user@example.com', 'proj-draft');
    expect(parsed).not.toBeNull();
    expect(parsed?.content).toBe('draft text to persist');
    expect(typeof parsed?.type).toBe('string');
  });

  test('composer is explicitly disabled when no project is selected', async () => {
    _mockMessagesFetch([]);
    const { container, getByText } = render(
      <MessagesPanel
        selectedProjectId={null}
        onClose={() => {}}
        addToast={() => {}}
        onMessageCountChange={() => {}}
      />,
    );

    const textarea = container.querySelector('textarea') as HTMLTextAreaElement;
    const sendBtn = container.querySelector('button[aria-label="Send message"]') as HTMLButtonElement;

    expect(textarea).toBeTruthy();
    expect(textarea.disabled).toBe(true);
    expect(textarea.placeholder).toBe('Select a project to message Strategic');
    expect(sendBtn).toBeTruthy();
    expect(sendBtn.disabled).toBe(true);
    expect(getByText('Select a project to message Strategic')).toBeTruthy();
  });

  test('draft is restored into the textarea when component remounts', async () => {
    // Pre-seed a draft for the project
    const draftKey = `messages_composer_draft:test-user@example.com:proj-draft`;
    localStorage.setItem(draftKey, JSON.stringify({ type: 'FEEDBACK', content: 'pre-existing draft' }));

    const { container } = renderDraftPanel();
    await waitFor(() => expect(_messagesGetCalls().length).toBeGreaterThanOrEqual(1));

    const textarea = container.querySelector('textarea') as HTMLTextAreaElement;
    expect(textarea).toBeTruthy();
    expect(textarea.value).toBe('pre-existing draft');
  });

  test('draft is cleared from localStorage after successful send', async () => {
    // Make the POST succeed (and all other requests as well)
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const initObj = init as RequestInit | undefined;
      const method = initObj?.method ?? 'GET';
      if (method === 'POST' && urlStr.includes('/api/admin/messages')) {
        return new Response(JSON.stringify({ arming: 'steering' }), { status: 200 });
      }
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (/\/api\/admin\/messages(\?|$)/.test(urlStr) && method === 'GET') {
        return new Response(JSON.stringify({ messages: [] }), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });

    _suppressSinceLastVisit('test-user@example.com', 'proj-draft');
    const { container } = render(
      <MessagesPanel
        selectedProjectId="proj-draft"
        onClose={() => {}}
        addToast={() => {}}
        onMessageCountChange={() => {}}
      />,
    );
    await waitFor(() => expect(_messagesGetCalls().length).toBeGreaterThanOrEqual(1));

    // Type content via fireEvent so the React state is properly set and the draft is saved
    const { fireEvent: fe2 } = await import('@testing-library/react');
    const textarea = container.querySelector('textarea') as HTMLTextAreaElement;
    await act(async () => {
      fe2.change(textarea, { target: { value: 'about to send' } });
    });

    await waitFor(() => expect(loadComposerDraft('test-user@example.com', 'proj-draft')).not.toBeNull());

    // Find the send button by its distinctive blue class and click it
    const sendBtn = container.querySelector('button.bg-blue-600') as HTMLButtonElement;
    expect(sendBtn).toBeTruthy();
    expect(sendBtn.disabled).toBe(false);

    await act(async () => {
      sendBtn.click();
    });

    await waitFor(() => {
      expect(loadComposerDraft('test-user@example.com', 'proj-draft')).toBeNull();
    });
  });

  test('minimal chat restores draft content but sends through the Strategic steer path', async () => {
    const draftKey = 'messages_composer_draft:test-user@example.com:proj-draft';
    localStorage.setItem(draftKey, JSON.stringify({ type: 'BLOCKER', content: 'use the clean chat path' }));
    const postedBodies: unknown[] = [];
    const sessionStartedAt = new Date(Date.now() - 30 * 60_000).toISOString();
    const lastInteractionAt = new Date(Date.now() - 5 * 60_000).toISOString();

    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const initObj = init as RequestInit | undefined;
      const method = initObj?.method ?? 'GET';
      if (method === 'POST' && /\/api\/admin\/projects\/[^/]+\/strategic-session\/messages$/.test(urlStr)) {
        postedBodies.push(JSON.parse(String(initObj?.body ?? '{}')));
        return new Response(JSON.stringify({ arming: 'steering' }), { status: 200 });
      }
      if (method === 'GET' && /\/api\/admin\/projects\/[^/]+\/strategic-conversation(?:\?|$)/.test(urlStr)) {
        return new Response(JSON.stringify({
          conversation: { limit: 20, has_more: false, order: 'oldest_first', items: [] },
        }), { status: 200 });
      }
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (/\/api\/admin\/messages(\?|$)/.test(urlStr) && method === 'GET') {
        return new Response(JSON.stringify({ messages: [] }), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });

    _suppressSinceLastVisit('test-user@example.com', 'proj-draft');
    const { container, getAllByRole } = render(
      <MessagesPanel
        selectedProjectId="proj-draft"
        variant="page"
        filterChrome="minimal"
        showCloseButton={false}
        addToast={() => {}}
        onMessageCountChange={() => {}}
        strategicState={makeStrategicState({
          project_id: 'proj-draft',
          context_package: {
            id: 'strategic-context:proj-draft',
            title: 'Draft workbench context',
            status: 'ready',
            summary: 'Pointer-safe package summary.',
            item_count: 3,
            token_count: null,
            sources: [],
            drilldowns: [],
            resources: [],
            canvas: {
              id: 'strategic-canvas:proj-draft',
              node_count: 2,
              edge_count: 1,
              nodes: [{ id: 'node-1', kind: 'task', label: 'Heavy canvas node' }],
              edges: [],
            },
          },
          work_session: {
            version: 1,
            id: 'strategic-session:proj-draft:2026-06-29T01:00:00Z',
            mode: 'continuous',
            active_surface: 'strategic',
            session_started_at: sessionStartedAt,
            last_interaction_at: lastInteractionAt,
            context_package_id: 'strategic-context:proj-draft',
            attention_level: 'watch',
            selected_refs: [
              { kind: 'knowledge_document', id: 'knowledge:kdoc-1', label: 'Operator notes' },
            ],
            selected_ref_descriptors: [
              {
                kind: 'artifact',
                id: 'artifact:latest-context',
                label: 'Latest context report',
                summary: 'Short pointer summary.',
                status: 'active',
              },
            ],
            curated_metrics: [],
          },
        })}
        strategicPollEpoch={1}
        strategicWorkbenchState={{
          version: 1,
          interaction_mode: 'continuous',
          active_surface: 'strategic',
          preferred_runner_id: null,
          selected_refs: [
            { kind: 'artifact', id: 'artifact:latest-context', label: 'Latest context report' },
          ],
          session_started_at: sessionStartedAt,
          last_interaction_at: lastInteractionAt,
        }}
      />,
    );
    // strategicState is supplied as a prop, so usesStrategicReadModel=true and
    // the component skips the strategic-conversation GET (piggybacking on the
    // poll epoch instead). Wait for the textarea to confirm the component mounted.
    await waitFor(() => expect(container.querySelector('textarea')).toBeTruthy());

    const textarea = container.querySelector('textarea') as HTMLTextAreaElement;
    expect(textarea.value).toBe('use the clean chat path');
    const sendButton = getAllByRole('button', { name: 'Send message' })
      .find((button): button is HTMLButtonElement => button instanceof HTMLButtonElement && !button.disabled);
    if (!sendButton) throw new Error('Expected enabled Strategic send button');
    expect(sendButton).toHaveAttribute('type', 'button');
    expect(container.textContent).not.toContain('Blocker');

    await act(async () => {
      sendButton?.click();
    });

    await waitFor(() => expect(postedBodies).toHaveLength(1));
    expect(_strategicSessionMessagePostCalls()).toHaveLength(1);
    expect(postedBodies[0]).toMatchObject({
      message_type: 'FEEDBACK',
      content: 'use the clean chat path',
      source_refs: [],
      artifact_refs: [],
    });
    expect(postedBodies[0]).not.toHaveProperty('workbench_context');
    expect(JSON.stringify(postedBodies[0])).not.toContain('Heavy canvas node');
    expect(JSON.stringify(postedBodies[0])).not.toContain('artifact:latest-context');
    expect(postedBodies[0]).not.toHaveProperty('project_id');
  });

  test('stale Strategic workbench state does not keep routing workbench_context', async () => {
    const staleNaive = new Date(Date.now() - 5 * 3600_000).toISOString().replace('Z', '');
    const postedBodies: unknown[] = [];

    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const initObj = init as RequestInit | undefined;
      const method = initObj?.method ?? 'GET';
      if (method === 'POST' && /\/api\/admin\/projects\/[^/]+\/strategic-session\/messages$/.test(urlStr)) {
        postedBodies.push(JSON.parse(String(initObj?.body ?? '{}')));
        return new Response(JSON.stringify({ arming: 'steering' }), { status: 200 });
      }
      if (method === 'GET' && /\/api\/admin\/projects\/[^/]+\/strategic-conversation(?:\?|$)/.test(urlStr)) {
        return new Response(JSON.stringify({
          conversation: { limit: 20, has_more: false, order: 'oldest_first', items: [] },
        }), { status: 200 });
      }
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (/\/api\/admin\/messages(\?|$)/.test(urlStr) && method === 'GET') {
        return new Response(JSON.stringify({ messages: [] }), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });

    _suppressSinceLastVisit('test-user@example.com', 'proj-draft');
    const { container } = render(
      <MessagesPanel
        selectedProjectId="proj-draft"
        variant="page"
        filterChrome="minimal"
        showCloseButton={false}
        addToast={() => {}}
        onMessageCountChange={() => {}}
        strategicState={makeStrategicState({
          context_package: null,
          work_session: {
            version: 1,
            id: 'strategic-session:proj-draft',
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
        })}
        strategicWorkbenchState={{
          version: 1,
          interaction_mode: 'continuous',
          active_surface: 'pipeline',
          selected_refs: [
            { kind: 'artifact', id: 'artifact:stale', label: 'Stale ref' },
          ],
          session_started_at: staleNaive,
          last_interaction_at: null,
        }}
      />,
    );
    // A supplied Strategic read model deliberately skips the conversation GET.
    // The mounted composer is the readiness signal for this direct-send path.
    await waitFor(() => expect(container.querySelector('textarea')).toBeTruthy());

    const textarea = container.querySelector('textarea') as HTMLTextAreaElement;
    expect(textarea.value).toBe('');

    const { fireEvent } = await import('@testing-library/react');
    await act(async () => {
      fireEvent.change(textarea, { target: { value: 'stale workbench should not route' } });
    });

    const sendButton = container.querySelector('button[aria-label="Send message"]') as HTMLButtonElement | null;
    if (!sendButton) throw new Error('Expected send button');
    await act(async () => {
      sendButton.click();
    });

    await waitFor(() => expect(postedBodies).toHaveLength(1));
    expect(_strategicSessionMessagePostCalls()).toHaveLength(1);
    expect(postedBodies[0]).not.toHaveProperty('workbench_context');
    expect(postedBodies[0]).not.toHaveProperty('project_id');
  });

  test('draft is scoped per project — switching projects loads the new project draft', async () => {
    // Seed separate drafts for two projects
    localStorage.setItem(
      'messages_composer_draft:test-user@example.com:proj-A',
      JSON.stringify({ type: 'FEEDBACK', content: 'draft for project A' }),
    );
    localStorage.setItem(
      'messages_composer_draft:test-user@example.com:proj-B',
      JSON.stringify({ type: 'NOTE', content: 'draft for project B' }),
    );

    _suppressSinceLastVisit('test-user@example.com', 'proj-A');
    _mockMessagesFetch([]);

    const { container, rerender } = render(
      <MessagesPanel
        selectedProjectId="proj-A"
        onClose={() => {}}
        addToast={() => {}}
        onMessageCountChange={() => {}}
      />,
    );
    await waitFor(() => expect(_messagesGetCalls().length).toBeGreaterThanOrEqual(1));

    const textarea = container.querySelector('textarea') as HTMLTextAreaElement;
    expect(textarea.value).toBe('draft for project A');

    // Switch to project B
    _suppressSinceLastVisit('test-user@example.com', 'proj-B');
    _mockMessagesFetch([]);
    const callsBefore = _messagesGetCalls().length;

    await act(async () => {
      rerender(
        <MessagesPanel
          selectedProjectId="proj-B"
          onClose={() => {}}
          addToast={() => {}}
          onMessageCountChange={() => {}}
        />,
      );
    });

    await waitFor(() => expect(_messagesGetCalls().length).toBeGreaterThan(callsBefore));

    expect(textarea.value).toBe('draft for project B');
  });
});

// ── Strategic cycle live-progress render tests ────────────────────────────────

describe('MessagesPanel strategic cycle progress indicators', () => {
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

  async function renderPanel(props: {
    strategistActive?: boolean;
    cycleProgress?: import('../hooks/useLogViewer').StrategicCycleProgress | null;
    lastCycleCompletion?: import('../hooks/useLogViewer').StrategicCycleCompletion | null;
  } = {}) {
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    _mockMessagesFetch([]);
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
          {...props}
        />,
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    return host as HTMLDivElement;
  }

  // ── Live progress indicator ────────────────────────────────────────────────

  test('shows generic "Strategist working…" when strategistActive and no phase label (minimal event backward-compat)', async () => {
    const el = await renderPanel({ strategistActive: true, cycleProgress: null });
    expect(el.textContent).toContain('Strategist working…');
    expect(el.textContent).not.toContain('Strategist:');
  });

  test('shows phase label from enriched event when available', async () => {
    const el = await renderPanel({
      strategistActive: true,
      cycleProgress: { task_id: 't1', phase_label: 'Spawning tasks' },
    });
    expect(el.textContent).toContain('Strategist: Spawning tasks…');
  });

  test('shows steering excerpt under the phase label', async () => {
    const el = await renderPanel({
      strategistActive: true,
      cycleProgress: {
        task_id: 't1',
        phase_label: 'Reviewing',
        steering_excerpt: 'Focus on backend reliability',
      },
    });
    expect(el.textContent).toContain('Strategist: Reviewing…');
    expect(el.textContent).toContain('Focus on backend reliability');
  });

  test('truncates steering excerpt longer than 60 chars', async () => {
    const longExcerpt = 'A'.repeat(120);
    const el = await renderPanel({
      strategistActive: true,
      cycleProgress: { task_id: 't1', steering_excerpt: longExcerpt },
    });
    // StrategistStatusChip intentionally keeps this conversational status compact.
    expect(el.textContent).toContain('A'.repeat(60) + '…');
    expect(el.textContent).not.toContain('A'.repeat(61));
  });

  test('does not show progress indicator when strategistActive is false', async () => {
    const el = await renderPanel({
      strategistActive: false,
      cycleProgress: { task_id: 't1', phase_label: 'Spawning tasks' },
    });
    expect(el.textContent).not.toContain('Strategist');
  });

  // ── Cycle completion receipt ────────────────────────────────────────────────

  test('shows "Cycle complete" banner with created direction count', async () => {
    const el = await renderPanel({
      strategistActive: false,
      lastCycleCompletion: { status: 'completed', created_count: 3 },
    });
    expect(el.textContent).toContain('Cycle complete');
    expect(el.textContent).toContain('+3 directions');
  });

  test('completion receipt shows retired and tasks spawned counts', async () => {
    const el = await renderPanel({
      strategistActive: false,
      lastCycleCompletion: { status: 'completed', retired_count: 2, tasks_spawned: 5 },
    });
    expect(el.textContent).toContain('−2 retired');
    expect(el.textContent).toContain('5 tasks spawned');
  });

  test('completion receipt shows staged directions count', async () => {
    const el = await renderPanel({
      strategistActive: false,
      lastCycleCompletion: { status: 'completed', staged_count: 3 },
    });
    expect(el.textContent).toContain('3 directions staged');
  });

  test('completion receipt shows running update and problem model flags', async () => {
    const el = await renderPanel({
      strategistActive: false,
      lastCycleCompletion: {
        status: 'completed',
        running_update_changed: true,
        problem_model_changed: true,
      },
    });
    expect(el.textContent).toContain('running update written');
    expect(el.textContent).toContain('problem model updated');
  });

  test('failed cycle shows "Cycle failed" with red accent, not emerald', async () => {
    const el = await renderPanel({
      strategistActive: false,
      lastCycleCompletion: { status: 'failed', failure_reason: 'Timed out after 5 minutes' },
    });
    expect(el.textContent).toContain('Cycle failed');
    expect(el.textContent).toContain('Timed out after 5 minutes');
    // The banner container should have red background, not emerald
    const banner = el.querySelector('[role="status"]');
    expect(banner?.className).toContain('red');
    expect(banner?.className).not.toContain('emerald');
  });

  test.each([
    ['deferred', 'Cycle deferred', 'amber'],
    ['FAILED', 'Cycle failed', 'red'],
    ['CANCELLED', 'Cycle cancelled', 'red'],
  ])('completion receipt renders %s status distinctly', async (status, label, tone) => {
    const el = await renderPanel({
      strategistActive: false,
      lastCycleCompletion: { status, failure_reason: status === 'deferred' ? undefined : 'boom' },
    });
    expect(el.textContent).toContain(label);
    const banner = el.querySelector('[role="status"]');
    expect(banner?.className).toContain(tone);
  });

  test('successful cycle shows emerald accent, not red', async () => {
    const el = await renderPanel({
      strategistActive: false,
      lastCycleCompletion: { status: 'completed', created_count: 1 },
    });
    expect(el.textContent).toContain('Cycle complete');
    const banner = el.querySelector('[role="status"]');
    expect(banner?.className).toContain('emerald');
    expect(banner?.className).not.toContain('red');
  });

  test('completion receipt is hidden while a new cycle is active', async () => {
    const el = await renderPanel({
      strategistActive: true,
      lastCycleCompletion: { status: 'completed', created_count: 2 },
    });
    // While active, show progress indicator; hide the old completion receipt
    expect(el.textContent).toContain('Strategist working…');
    expect(el.textContent).not.toContain('Cycle complete');
  });

  test('completion receipt is absent when lastCycleCompletion is null', async () => {
    const el = await renderPanel({ lastCycleCompletion: null });
    expect(el.textContent).not.toContain('Cycle complete');
    expect(el.textContent).not.toContain('Cycle failed');
  });

  test('completion receipt shows compact strategic challenge summary when present', async () => {
    const el = await renderPanel({
      strategistActive: false,
      lastCycleCompletion: {
        status: 'completed',
        created_count: 1,
        strategic_judgment_challenge: {
          mode: 'ok',
          verdict: 'warned',
          warned_count: 2,
          blocked_count: 1,
        },
      },
    });
    expect(el.textContent).toContain('Cycle complete');
    // Compact chip surfaces verdict and counts without the full item list.
    const chip = el.querySelector('[aria-label*="Strategic judgment challenge"]');
    expect(chip).not.toBeNull();
    expect(chip?.textContent).toContain('challenge: ok · warned');
    expect(chip?.textContent).toContain('1 blocked');
    expect(chip?.textContent).toContain('2 warned');
  });

  test('completion receipt shows no challenge chip (and no warning styling) when summary absent', async () => {
    const el = await renderPanel({
      strategistActive: false,
      lastCycleCompletion: { status: 'completed', created_count: 1 },
    });
    expect(el.textContent).toContain('Cycle complete');
    // Pre-challenge cycles carry no summary → no chip rendered at all.
    expect(el.querySelector('[aria-label*="Strategic judgment challenge"]')).toBeNull();
    expect(el.textContent).not.toContain('challenge:');
  });
});

// ── convert-to-direction modal ────────────────────────────────────────────────

describe('MessagesPanel convert-to-direction modal', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
  });

  afterEach(() => {
    const r = root;
    if (r) act(() => r.unmount());
    root = null;
    host?.remove();
    host = null;
    localStorage.clear();
    vi.clearAllMocks();
  });

  function makeApiFetchWithConvertSupport(feedbackMsg: ProjectMessage, opts: { directionsOk?: boolean; resolveOk?: boolean } = {}) {
    const { directionsOk = true, resolveOk = true } = opts;
    return async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const initObj = init as RequestInit | undefined;
      const method = initObj?.method ?? 'GET';

      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (/\/api\/admin\/messages(\?|$)/.test(urlStr) && method === 'GET') {
        return new Response(JSON.stringify({ messages: [feedbackMsg] }), { status: 200 });
      }
      if (method === 'GET' && (
        urlStr.includes('/api/admin/messages/noise-prefs') ||
        urlStr.includes('/api/admin/messages/saved-views') ||
        urlStr.includes('/api/admin/messages/temp-mutes') ||
        urlStr.includes('/api/admin/messages/mute-window')
      )) {
        return new Response('', { status: 404 });
      }
      if (urlStr.includes('/directions') && method === 'POST') {
        if (!directionsOk) return new Response('', { status: 500 });
        return new Response(JSON.stringify({ direction: { id: 'dir-new-111' } }), { status: 200 });
      }
      if (/\/api\/admin\/messages\/fb-msg-1/.test(urlStr) && method === 'PATCH') {
        if (!resolveOk) return new Response('', { status: 500 });
        return new Response(JSON.stringify({}), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    };
  }

  test('handleOpenConvertModal seeds the modal with the message content', async () => {
    const feedbackContent = 'Please add GPU support to the runner fleet';
    const feedback = makeMsg({
      id: 'fb-msg-1', project_id: 'proj-convert-1',
      message_type: 'FEEDBACK', from_type: 'agent', content: feedbackContent,
    });
    _suppressSinceLastVisit('test-user@example.com', 'proj-convert-1');
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(makeApiFetchWithConvertSupport(feedback));

    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <MessagesPanel
          selectedProjectId="proj-convert-1"
          onClose={() => {}}
          addToast={() => {}}
          onMessageCountChange={() => {}}
        />
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    const el = host as HTMLDivElement;

    // Click the "Create Direction" button (PlusCircle, title="Create Direction")
    const createDirBtn = [...el.querySelectorAll('button')].find(b => b.getAttribute('title') === 'Create Direction');
    expect(createDirBtn).not.toBeNull();
    await act(async () => { (createDirBtn as HTMLButtonElement).click(); });

    // Modal should open with the message content seeded in the text input
    const dialog = el.querySelector('[role="dialog"]');
    expect(dialog).not.toBeNull();
    expect(dialog!.textContent).toContain('Create direction from message');

    // DirectionsAddForm uses an <input> (not <textarea>) for the main direction text.
    // Verify it is seeded with the feedback message content.
    const textInput = el.querySelector('input[placeholder="New direction..."]') as HTMLInputElement | null;
    expect(textInput).not.toBeNull();
    expect(textInput!.value).toBe(feedbackContent);
  });

  test('handleSubmitConvertForm success: POSTs direction, PATCHes resolution, closes modal', async () => {
    const feedbackContent = 'Add better error messages to the compute job runner';
    const feedback = makeMsg({
      id: 'fb-msg-1', project_id: 'proj-convert-1',
      message_type: 'FEEDBACK', from_type: 'agent', content: feedbackContent,
    });
    _suppressSinceLastVisit('test-user@example.com', 'proj-convert-1');
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(makeApiFetchWithConvertSupport(feedback));

    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <MessagesPanel
          selectedProjectId="proj-convert-1"
          onClose={() => {}}
          addToast={() => {}}
          onMessageCountChange={() => {}}
        />
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    const el = host as HTMLDivElement;

    // Open the modal
    const createDirBtn = [...el.querySelectorAll('button')].find(b => b.getAttribute('title') === 'Create Direction');
    await act(async () => { (createDirBtn as HTMLButtonElement).click(); });

    // Submit the form via the "Create direction" button in the modal footer
    const submitBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Create direction');
    expect(submitBtn).not.toBeNull();
    await act(async () => { (submitBtn as HTMLButtonElement).click(); });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });

    // Verify the direction POST was called
    const postCalls = vi.mocked(apiFetch).mock.calls.filter(c =>
      String(c[0]).includes('/directions') && (c[1] as RequestInit | undefined)?.method === 'POST'
    );
    expect(postCalls).toHaveLength(1);
    const postedBody = JSON.parse((postCalls[0][1] as RequestInit).body as string);
    expect(postedBody.text).toBe(feedbackContent);

    // Verify the PATCH to resolve the message was called
    const patchCalls = vi.mocked(apiFetch).mock.calls.filter(c =>
      String(c[0]).includes('/api/admin/messages/fb-msg-1') && (c[1] as RequestInit | undefined)?.method === 'PATCH'
    );
    expect(patchCalls).toHaveLength(1);
    const patchBody = JSON.parse((patchCalls[0][1] as RequestInit).body as string);
    expect(patchBody.resolution).toContain('dir-new-111');

    // Modal should be closed after success
    expect(el.querySelector('[role="dialog"]')).toBeNull();
  });

  test('handleSubmitConvertForm error: markAddAttemptFailed is called and modal stays open', async () => {
    const feedbackContent = 'Improve task scheduling fairness';
    const feedback = makeMsg({
      id: 'fb-msg-1', project_id: 'proj-convert-1',
      message_type: 'FEEDBACK', from_type: 'agent', content: feedbackContent,
    });
    _suppressSinceLastVisit('test-user@example.com', 'proj-convert-1');
    // Directions POST fails with 500
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(
      makeApiFetchWithConvertSupport(feedback, { directionsOk: false })
    );

    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <MessagesPanel
          selectedProjectId="proj-convert-1"
          onClose={() => {}}
          addToast={() => {}}
          onMessageCountChange={() => {}}
        />
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    const el = host as HTMLDivElement;

    // Open the modal
    const createDirBtn = [...el.querySelectorAll('button')].find(b => b.getAttribute('title') === 'Create Direction');
    await act(async () => { (createDirBtn as HTMLButtonElement).click(); });

    // Submit the form
    const submitBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Create direction');
    await act(async () => { (submitBtn as HTMLButtonElement).click(); });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });

    // Modal should stay open on failure
    const dialog = el.querySelector('[role="dialog"]');
    expect(dialog).not.toBeNull();
    expect(dialog!.textContent).toContain('Create direction from message');

    // Verify the direction POST was attempted
    const postCalls = vi.mocked(apiFetch).mock.calls.filter(c =>
      String(c[0]).includes('/directions') && (c[1] as RequestInit | undefined)?.method === 'POST'
    );
    expect(postCalls).toHaveLength(1);

    // Idempotency key should be retained for retry: submitting again should reuse the same key
    // Re-enable successful responses for the retry
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(makeApiFetchWithConvertSupport(feedback));
    await act(async () => { (submitBtn as HTMLButtonElement).click(); });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });

    const allPostCalls = vi.mocked(apiFetch).mock.calls.filter(c =>
      String(c[0]).includes('/directions') && (c[1] as RequestInit | undefined)?.method === 'POST'
    );
    // Two POST attempts total
    expect(allPostCalls).toHaveLength(2);
    const firstKey = JSON.parse((allPostCalls[0][1] as RequestInit).body as string).idempotency_key;
    const secondKey = JSON.parse((allPostCalls[1][1] as RequestInit).body as string).idempotency_key;
    // Retry must reuse the same key (idempotency key is retained on failure)
    expect(firstKey).toBe(secondKey);
  });

  test('closing the modal via Cancel resets the form', async () => {
    const feedbackContent = 'Fix memory leak in worker threads';
    const feedback = makeMsg({
      id: 'fb-msg-1', project_id: 'proj-convert-1',
      message_type: 'FEEDBACK', from_type: 'agent', content: feedbackContent,
    });
    _suppressSinceLastVisit('test-user@example.com', 'proj-convert-1');
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(makeApiFetchWithConvertSupport(feedback));

    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <MessagesPanel
          selectedProjectId="proj-convert-1"
          onClose={() => {}}
          addToast={() => {}}
          onMessageCountChange={() => {}}
        />
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    const el = host as HTMLDivElement;

    // Open the modal
    const createDirBtn = [...el.querySelectorAll('button')].find(b => b.getAttribute('title') === 'Create Direction');
    await act(async () => { (createDirBtn as HTMLButtonElement).click(); });
    expect(el.querySelector('[role="dialog"]')).not.toBeNull();

    // Cancel the modal
    const cancelBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Cancel');
    expect(cancelBtn).not.toBeNull();
    await act(async () => { (cancelBtn as HTMLButtonElement).click(); });

    // Modal should be closed
    expect(el.querySelector('[role="dialog"]')).toBeNull();
  });
});
