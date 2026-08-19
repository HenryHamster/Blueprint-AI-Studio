import { describe, test, expect, vi, afterEach, beforeEach } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { fireEvent, waitFor } from '@testing-library/react';
import { MessagesPanel as MessagesPanelComponent, type MessagesPanelProps } from './MessagesPanel';

import type { ProjectMessage } from './MessageBubble';
import { armingToastCopy, armingHintCopy } from './messageArming';
import { auth } from '../firebase';
import { loadComposerDraft } from '../utils/conversationDraftState';
import type { StrategicStateResponse } from '../strategicStateApi';

type MessagesPanelTestProps = Omit<MessagesPanelProps, 'canOperateProject'> & { canOperateProject?: boolean };
function MessagesPanel(props: MessagesPanelTestProps) {
  return <MessagesPanelComponent canOperateProject={true} {...props} />;
}

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
vi.mock('../utils/pendingAttachmentStore', () => {
  type Stored = { id: string; file: File; mime_type: string; name: string; size_bytes: number };
  const records = new Map<string, Stored>();
  const prefix = (actor: string, project: string, send: string) => `${actor}:${project}:${send}:`;
  const key = (actor: string, project: string, send: string, id: string) => `${prefix(actor, project, send)}${id}`;
  return {
    putPendingAttachment: vi.fn(async (actor: string, project: string, send: string, item: Stored) => {
      records.set(key(actor, project, send, item.id), item);
    }),
    deletePendingAttachment: vi.fn(async (actor: string, project: string, send: string, id: string) => {
      records.delete(key(actor, project, send, id));
    }),
    clearPendingAttachments: vi.fn(async (actor: string, project: string, send: string) => {
      for (const recordKey of records.keys()) {
        if (recordKey.startsWith(prefix(actor, project, send))) records.delete(recordKey);
      }
    }),
    listPendingAttachments: vi.fn(async (actor: string, project: string, send: string) =>
      [...records.entries()]
        .filter(([recordKey]) => recordKey.startsWith(prefix(actor, project, send)))
        .map(([, item]) => item)),
    retryPendingAttachmentCleanups: vi.fn(async () => undefined),
    resetPendingAttachmentMemoryStoreForTests: vi.fn(() => records.clear()),
  };
});

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>();
  return {
    ...actual,
    apiFetch: vi.fn(async () => new Response(JSON.stringify({ messages: [] }), { status: 200 })),
  };
});

import { apiFetch } from '../api';
import * as pendingAttachmentStore from '../utils/pendingAttachmentStore';

// jsdom does not implement scrollIntoView; several panel actions invoke it
// (summary card → routine section scroll, showOnlyNew → scroll to first new
// bubble). Stub it module-wide so click handlers do not throw and surface as
// unhandled errors in the vitest run.
if (typeof Element !== 'undefined' && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function () { /* no-op for jsdom */ };
}

// no message reads as "new", and the since-last-visit summary card / divider
// don't appear — useful for tests that pre-date those overlays.
function _suppressSinceLastVisit(userEmail: string, projectId: string): void {
  const farFuture = Date.now() + 365 * 24 * 60 * 60 * 1000;
  localStorage.setItem(`messages_panel_last_seen:${userEmail}:${projectId}`, String(farFuture));
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

function strategicStateForActionMessage(messageId: string): StrategicStateResponse {
  const state = makeStrategicState();
  const attention = state.attention;
  const item = attention?.items[0];
  if (!attention || !item) return state;
  state.attention = {
    ...attention,
    items: [{
      ...item,
      id: `message:${messageId}`,
      ref: { kind: 'message', id: messageId, project_id: 'proj-1' },
    }],
  };
  return state;
}


// and the composer hint then reflects that real outcome rather than the
// type-only guess, resetting to the prediction when the operator switches type.
// (The arming helpers are unit-tested in isolation in messageArming.test.ts;
// these tests lock the full send round-trip through the real MessagesPanel.)

describe('MessagesPanel send arming receipt', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    localStorage.clear();
    pendingAttachmentStore.resetPendingAttachmentMemoryStoreForTests();
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

  function backendLikeSendResponse(postBody: Record<string, unknown>): Record<string, unknown> {
    const arming = typeof postBody.arming === 'string' ? postBody.arming : null;
    const messageId = typeof postBody.id === 'string' ? postBody.id : 'm-send-response';
    const messageOverride = postBody.message && typeof postBody.message === 'object'
      ? postBody.message as Record<string, unknown>
      : {};
    const response: Record<string, unknown> = {
      id: messageId,
      status: arming === 'duplicate' ? 'duplicate' : 'created',
      message: {
        id: messageId,
        project_id: 'proj-1',
        message_type: 'FEEDBACK',
        from_type: 'user',
        from_task_id: null,
        content: 'Operator message',
        context: {},
        status: 'open',
        resolution: null,
        resolved_at: null,
        created_at: '2026-07-01T12:00:00Z',
        ...messageOverride,
      },
      ...postBody,
    };
    return response;
  }

  // Serves the panel's mount-time GETs normally (empty message list, no
  // last-seen, 404'd sync endpoints) and returns a backend-like create response
  // for POST /api/admin/messages. Pass `{}` to model an older server that omits
  // the `arming` field while still returning the durable message envelope.
  function _setupSendMock(postBody: Record<string, unknown>): void {
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (/\/api\/admin\/messages(\?|$)/.test(urlStr) && method === 'POST') {
        return new Response(JSON.stringify(backendLikeSendResponse(postBody)), { status: 200 });
      }
      if (/\/api\/admin\/messages(\?|$)/.test(urlStr) && method === 'GET') {
        return new Response(JSON.stringify({ messages: [] }), { status: 200 });
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

  function _setupQuestionAnswerMock(question: ProjectMessage, patchBodies: unknown[] = []): void {
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (urlStr.includes(`/api/admin/messages/${question.id}`) && method === 'PATCH') {
        patchBodies.push(JSON.parse(String((init as RequestInit | undefined)?.body ?? '{}')));
        return new Response(JSON.stringify({ status: 'ok' }), { status: 200 });
      }
      if (/\/api\/admin\/messages(\?|$)/.test(urlStr) && method === 'GET') {
        return new Response(JSON.stringify({ messages: [question] }), { status: 200 });
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

  async function renderPanel(
    addToast: (msg: string, level: string) => void,
    strategistActive = false,
    props: Partial<MessagesPanelProps> = {},
  ) {
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
          addToast={addToast as never}
          onMessageCountChange={() => {}}
          strategistActive={strategistActive}
          {...props}
        />,
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    return host as HTMLDivElement;
  }

  // The composer only ever renders an inline hint <p> for the not_armed outcome
  // (a genuine delivery failure); every other outcome is toast-only.
  const NOT_ARMED_HINT = armingHintCopy('not_armed');
  function hintText(el: HTMLElement): string | null {
    const p = [...el.querySelectorAll('p')].find(p => p.textContent === NOT_ARMED_HINT);
    return p?.textContent ?? null;
  }

  // Type into the composer textarea (controlled input — bypass React's value
  // tracker via the prototype setter so the dispatched event fires onChange) and
  // click Send.
  async function typeAndSend(el: HTMLElement, text: string) {
    const textarea = el.querySelector('textarea[placeholder="Type a message..."]') as HTMLTextAreaElement;
    expect(textarea).toBeTruthy();
    const nativeSetter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype, 'value',
    )!.set!;
    await act(async () => {
      nativeSetter.call(textarea, text);
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
    });
    const sendBtn = el.querySelector('button[class*="bg-blue-600"]') as HTMLButtonElement;
    expect(sendBtn).toBeTruthy();
    expect(sendBtn.disabled).toBe(false);
    await act(async () => { sendBtn.click(); });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 0)); });
  }

  test('steering send shows a success toast confirming the strategist was steered', async () => {
    _setupSendMock({ arming: 'steering' });
    const addToast = vi.fn();
    const el = await renderPanel(addToast);
    await typeAndSend(el, 'please prioritize the migration work');

    await waitFor(() => expect(addToast).toHaveBeenCalledTimes(1));
    const [text, severity] = addToast.mock.calls[0];
    expect(text).toBe(armingToastCopy('steering'));
    expect(text).toContain('steering the strategist');
    expect(severity).toBe('success');
    // steering is steady-state narration: no inline composer hint for it.
    expect(hintText(el)).toBeNull();
  });

  test('steering send refreshes Strategic state and shows pending badge immediately', async () => {
    _setupSendMock({ arming: 'steering' });
    const addToast = vi.fn();
    const onStrategicStateRefresh = vi.fn();
    const el = await renderPanel(addToast, false, {
      strategicState: makeStrategicState(),
      onStrategicStateRefresh,
    });

    await typeAndSend(el, 'please prioritize the migration work');

    await waitFor(() => expect(addToast).toHaveBeenCalledTimes(1));
    expect(onStrategicStateRefresh).toHaveBeenCalledTimes(1);
    expect(el.textContent).toContain('Strategic cycle pending');
    expect(el.textContent).not.toContain('please prioritize the migration work');
  });

  test('fresh optimistic steering badge survives a stale read-model response', async () => {
    _setupSendMock({ arming: 'steering' });
    const addToast = vi.fn();
    const onStrategicStateRefresh = vi.fn();
    const el = await renderPanel(addToast, false, {
      strategicState: makeStrategicState(),
      onStrategicStateRefresh,
    });

    await typeAndSend(el, 'please prioritize the migration work');
    await waitFor(() => expect(el.textContent).toContain('Strategic cycle pending'));

    await act(async () => {
      root!.render(
        <MessagesPanel
          selectedProjectId="proj-1"
          onClose={() => {}}
          addToast={addToast as never}
          onMessageCountChange={() => {}}
          strategicState={makeStrategicState()}
          onStrategicStateRefresh={onStrategicStateRefresh}
        />,
      );
    });

    expect(el.textContent).toContain('Strategic cycle pending');
    expect(el.textContent).not.toContain('please prioritize the migration work');
  });

  test('fresh optimistic steering badge survives an older read-model marker', async () => {
    _setupSendMock({ arming: 'steering' });
    const addToast = vi.fn();
    const staleMarkerAt = new Date(Date.now() - 60_000).toISOString();
    const onStrategicStateRefresh = vi.fn();
    const el = await renderPanel(addToast, false, {
      strategicState: makeStrategicState(),
      onStrategicStateRefresh,
    });

    await typeAndSend(el, 'please prioritize the migration work');
    await waitFor(() => expect(el.textContent).toContain('Strategic cycle pending'));

    await act(async () => {
      root!.render(
        <MessagesPanel
          selectedProjectId="proj-1"
          onClose={() => {}}
          addToast={addToast as never}
          onMessageCountChange={() => {}}
          strategicState={makeStrategicState({
            strategic: {
              ...makeStrategicState().strategic,
              refresh: {
                pending: {
                  requested_at: staleMarkerAt,
                  origin: 'operator',
                  excerpt: 'older pre-send marker',
                  operator_count: 1,
                },
                last_fired_at: null,
                last_fired_origin: null,
              },
            },
          })}
          onStrategicStateRefresh={onStrategicStateRefresh}
        />,
      );
    });

    expect(el.textContent).toContain('Strategic cycle pending');
    expect(el.textContent).not.toContain('please prioritize the migration work');
    expect(el.textContent).not.toContain('older pre-send marker');
    expect(el.textContent).not.toContain('older pre-send marker');
  });

  test('answering a question refreshes Strategic state for attention counts', async () => {
    const sessionStartedAt = new Date(Date.now() - 30 * 60_000).toISOString();
    const lastInteractionAt = new Date(Date.now() - 5 * 60_000).toISOString();
    const question = makeMsg({
      id: 'q-refresh',
      message_type: 'QUESTION',
      from_type: 'agent',
      content: 'Should we keep the local runner active?',
      status: 'open',
    });
    const patchBodies: unknown[] = [];
    _setupQuestionAnswerMock(question, patchBodies);
    const addToast = vi.fn();
    const onStrategicStateRefresh = vi.fn();
    const onStrategicInteraction = vi.fn();
    const el = await renderPanel(addToast, false, {
      strategicState: strategicStateForActionMessage('q-refresh'),
        strategicWorkbenchState: {
          version: 1,
          interaction_mode: 'continuous',
          active_surface: 'strategic',
          selected_refs: [
            { kind: 'knowledge_document', id: 'knowledge:kdoc-1', label: 'Operator notes' },
          ],
          session_started_at: sessionStartedAt,
          last_interaction_at: lastInteractionAt,
        },
      onStrategicStateRefresh,
      onStrategicInteraction,
    });

    const replyBtn = [...el.querySelectorAll('button')].find(button => button.title === 'Reply');
    expect(replyBtn).toBeTruthy();
    await act(async () => { (replyBtn as HTMLButtonElement).click(); });
    await typeAndSend(el, 'yes, keep it active');

    await waitFor(() => expect(addToast).toHaveBeenCalledWith('Question answered', 'success'));
    expect(patchBodies[0]).toMatchObject({
      resolution: 'yes, keep it active',
      workbench_context: {
        version: 1,
        interaction_mode: 'continuous',
        active_surface: 'strategic',
        selected_refs: [
          { kind: 'knowledge_document', id: 'knowledge:kdoc-1', label: 'Operator notes' },
        ],
        session_started_at: sessionStartedAt,
      },
    });
    expect(onStrategicStateRefresh).toHaveBeenCalledTimes(1);
    expect(onStrategicInteraction).toHaveBeenCalledTimes(1);
  });

  test('answering a question with attachments is blocked without dropping the attachment', async () => {
    const question = makeMsg({
      id: 'q-attachment',
      message_type: 'QUESTION',
      from_type: 'agent',
      content: 'Can you inspect the screenshot?',
      status: 'open',
    });
    const patchBodies: unknown[] = [];
    _setupQuestionAnswerMock(question, patchBodies);
    const addToast = vi.fn();
    const el = await renderPanel(addToast);

    const replyBtn = [...el.querySelectorAll('button')].find(button => button.title === 'Reply');
    expect(replyBtn).toBeTruthy();
    await act(async () => { (replyBtn as HTMLButtonElement).click(); });

    const fileInput = el.querySelector('input[type="file"]') as HTMLInputElement;
    expect(fileInput).toBeTruthy();
    await act(async () => {
      Object.defineProperty(fileInput, 'files', {
        value: [new File(['fake-png'], 'question-screenshot.png', { type: 'image/png' })],
        configurable: true,
      });
      fileInput.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });

    await typeAndSend(el, 'see attached');

    await waitFor(() => {
      expect(addToast).toHaveBeenCalledWith('Remove attachments before answering a question', 'error');
    });
    expect(patchBodies).toHaveLength(0);
    expect(el.textContent).toContain('question-screenshot.png');
  });

  test('passive_note send shows a success "won’t wake" toast', async () => {
    _setupSendMock({ arming: 'passive_note' });
    const addToast = vi.fn();
    const el = await renderPanel(addToast);
    await typeAndSend(el, 'just a note for the record');

    await waitFor(() => expect(addToast).toHaveBeenCalledTimes(1));
    const [text, severity] = addToast.mock.calls[0];
    expect(text).toBe(armingToastCopy('passive_note'));
    expect(text).toContain('won’t wake the strategist');
    expect(severity).toBe('success');
    // passive_note is steady-state narration: no inline composer hint for it.
    expect(hintText(el)).toBeNull();
  });

  test('successful send records the Strategic interaction', async () => {
    _setupSendMock({ arming: 'passive_note' });
    const addToast = vi.fn();
    const onStrategicInteraction = vi.fn();
    const el = await renderPanel(addToast, false, { onStrategicInteraction });

    await typeAndSend(el, 'record this in the current session');

    await waitFor(() => expect(addToast).toHaveBeenCalledTimes(1));
    expect(onStrategicInteraction).toHaveBeenCalledTimes(1);
  });

  test('routine_event send shows a success "won’t wake" routine toast', async () => {
    _setupSendMock({ arming: 'routine_event' });
    const addToast = vi.fn();
    const el = await renderPanel(addToast);
    await typeAndSend(el, 'routine bookkeeping');

    await waitFor(() => expect(addToast).toHaveBeenCalledTimes(1));
    const [text, severity] = addToast.mock.calls[0];
    expect(text).toBe(armingToastCopy('routine_event'));
    expect(text).toContain('routine event');
    expect(severity).toBe('success');
    // routine_event is steady-state narration: no inline composer hint for it.
    expect(hintText(el)).toBeNull();
  });

  test('not_armed send shows a non-success "wasn’t woken" receipt — NOT a false steering promise', async () => {
    _setupSendMock({ arming: 'not_armed' });
    const addToast = vi.fn();
    const el = await renderPanel(addToast);
    await typeAndSend(el, 'urgent: stop the current direction');

    await waitFor(() => expect(addToast).toHaveBeenCalledTimes(1));
    const [text, severity] = addToast.mock.calls[0];
    // The load-bearing truthfulness guarantee: a steering message the server
    // could not arm is surfaced as info, with copy that says the strategist was
    // NOT woken — never the green "steering the strategist" success line.
    expect(text).toBe(armingToastCopy('not_armed'));
    expect(text).toContain('wasn’t woken');
    expect(text).toContain('try resending');
    expect(text).not.toContain('steering the strategist');
    expect(severity).toBe('info');
    expect(severity).not.toBe('success');
    const hintEl = [...el.querySelectorAll('p')].find(p => p.textContent === armingHintCopy('not_armed'));
    expect(hintEl).toBeTruthy();
    expect(hintEl?.getAttribute('role')).toBe('alert');
    expect(hintEl?.className).toContain('text-amber-400');
  });

  test('no other outcome leaves an inline composer hint behind, before or after send', async () => {
    // Default type is FEEDBACK. No predictive pre-send hint is shown, and the
    // server-reported routine_event outcome (steady-state narration) is
    // toast-only — the composer stays free of it.
    _setupSendMock({ arming: 'routine_event' });
    const addToast = vi.fn();
    const el = await renderPanel(addToast);
    expect(hintText(el)).toBeNull();

    await typeAndSend(el, 'message that the server treats as routine');
    await waitFor(() => expect(addToast).toHaveBeenCalledTimes(1));

    expect(hintText(el)).toBeNull();
  });

  test('changing message type clears a stale not_armed warning', async () => {
    _setupSendMock({ arming: 'not_armed' });
    const addToast = vi.fn();
    const el = await renderPanel(addToast);
    await typeAndSend(el, 'urgent steering attempt');
    await waitFor(() => expect(addToast).toHaveBeenCalledTimes(1));
    // After the send the not_armed warning is visible.
    expect(hintText(el)).toBe(armingHintCopy('not_armed'));

    // Switch the message type to Note via the composer dropdown. Selecting a new
    // type clears the last-arming memory, so the stale warning disappears —
    // there is no predictive replacement hint to fall back to.
    const typeBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Feedback');
    expect(typeBtn).toBeTruthy();
    await act(async () => { (typeBtn as HTMLButtonElement).click(); });
    const noteOpt = [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Note');
    expect(noteOpt).toBeTruthy();
    await act(async () => { (noteOpt as HTMLButtonElement).click(); });

    expect(hintText(el)).toBeNull();
  });

  test('response missing the arming field falls back to the generic confirmation without error', async () => {
    _setupSendMock({}); // older server: no `arming` field
    const addToast = vi.fn();
    const el = await renderPanel(addToast);
    await typeAndSend(el, 'message to a server that omits arming');

    await waitFor(() => expect(addToast).toHaveBeenCalledTimes(1));
    const [text, severity] = addToast.mock.calls[0];
    expect(text).toBe(armingToastCopy(null));
    expect(text).toBe('Message sent');
    // No arming → generic success, and never a failure toast.
    expect(severity).toBe('success');
    expect(text).not.toBe('Failed to send message');
    // No arming is not a not_armed failure — no inline warning shown.
    expect(hintText(el)).toBeNull();
  });

  test('a failed send and its retry reuse one idempotency key, so a double-submit is one logical message', async () => {
    const postKeys: unknown[] = [];
    let postCount = 0;
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (/\/api\/admin\/messages(\?|$)/.test(urlStr) && method === 'POST') {
        const body = JSON.parse(String((init as RequestInit).body));
        postKeys.push(body.idempotency_key);
        postCount += 1;
        // First attempt fails server-side → the draft's key must be retained so
        // the retry carries the SAME key and the backend dedups it.
        if (postCount === 1) return new Response('boom', { status: 500 });
        return new Response(
          JSON.stringify({ id: 'm1', status: 'created', arming: 'steering' }),
          { status: 200 },
        );
      }
      if (/\/api\/admin\/messages(\?|$)/.test(urlStr) && method === 'GET') {
        return new Response(JSON.stringify({ messages: [] }), { status: 200 });
      }
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      return new Response('', { status: 404 });
    });
    const addToast = vi.fn();
    const el = await renderPanel(addToast);

    // First send fails — content is retained (only cleared on success).
    await typeAndSend(el, 'please prioritize the migration work');
    await waitFor(() => expect(addToast).toHaveBeenCalled());

    // Retry the SAME draft: the key must match so it collapses to one message.
    const sendBtn = el.querySelector('button[class*="bg-blue-600"]') as HTMLButtonElement;
    expect(sendBtn.disabled).toBe(false);
    await act(async () => { sendBtn.click(); });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 0)); });

    expect(postKeys.length).toBe(2);
    expect(postKeys[0]).toBeTruthy();
    expect(postKeys[0]).toBe(postKeys[1]);

    // A subsequent distinct message mints a FRESH key (no false dedup).
    await typeAndSend(el, 'a different steering message');
    expect(postKeys.length).toBe(3);
    expect(postKeys[2]).toBeTruthy();
    expect(postKeys[2]).not.toBe(postKeys[0]);
  });

  test('refreshes strategic state after steering message mutation', async () => {
    _setupSendMock({ arming: 'steering' });
    const addToast = vi.fn();
    const onStrategicRefresh = vi.fn();
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
          addToast={addToast as never}
          onMessageCountChange={() => {}}
          onStrategicRefresh={onStrategicRefresh}
        />,
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    const el = host as HTMLDivElement;

    expect(onStrategicRefresh).not.toHaveBeenCalled();
    await typeAndSend(el, 'steer: focus on the migration work');
    await waitFor(() => expect(addToast).toHaveBeenCalledTimes(1));
    expect(onStrategicRefresh).toHaveBeenCalledTimes(1);
  });

  test('attachment upload flow: type=image + signed download URL in context', async () => {
    const uploadUrlCalls: Array<{ body: unknown }> = [];
    const messagePostCalls: Array<{ body: unknown }> = [];

    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      const body = (init as RequestInit | undefined)?.body;
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (urlStr.includes('/attachment-upload-url') && method === 'POST') {
        uploadUrlCalls.push({ body: body ? JSON.parse(String(body)) : null });
        return new Response(JSON.stringify({
          upload_url: 'https://storage.googleapis.com/signed-upload',
          download_url: 'https://storage.googleapis.com/signed-download',
          storage_uri: 'gs://bucket/message-attachments/proj-1/att-123/test.png',
          attachment_id: 'att-123',
          size_bytes: JSON.parse(String(body)).size_bytes,
        }), { status: 200 });
      }
      if (/\/api\/admin\/messages$/.test(urlStr) && method === 'POST') {
        messagePostCalls.push({ body: body ? JSON.parse(String(body)) : null });
        return new Response(JSON.stringify({ id: 'm1', status: 'created', arming: 'steering' }), { status: 200 });
      }
      if (/\/api\/admin\/messages(\?|$)/.test(urlStr) && method === 'GET') {
        return new Response(JSON.stringify({ messages: [] }), { status: 200 });
      }
      return new Response('', { status: 404 });
    });

    const origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url) === 'https://storage.googleapis.com/signed-upload') {
        return new Response(null, { status: 200 });
      }
      return origFetch(url);
    });

    const addToast = vi.fn();
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
          addToast={addToast as never}
          onMessageCountChange={() => {}}
        />,
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    const el = host as HTMLDivElement;

    // Select an image file via the file input.
    const fileInput = el.querySelector('input[type="file"]') as HTMLInputElement;
    expect(fileInput).toBeTruthy();
    const testFile = new File(['fake-png'], 'test-screenshot.png', { type: 'image/png' });
    await act(async () => {
      Object.defineProperty(fileInput, 'files', {
        value: [testFile],
        configurable: true,
      });
      fileInput.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });

    // Attachment preview renders with file name.
    expect(el.textContent).toContain('test-screenshot.png');

    // Remove the attachment and verify it disappears.
    const removeBtn = el.querySelector('button[aria-label="Remove test-screenshot.png"]') as HTMLButtonElement;
    expect(removeBtn).toBeTruthy();
    await act(async () => { removeBtn.click(); });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 0)); });
    expect(el.textContent).not.toContain('test-screenshot.png');

    // Re-add by redefining files (configurable: true allows re-definition).
    await act(async () => {
      Object.defineProperty(fileInput, 'files', {
        value: [new File(['fake-png'], 'test-screenshot.png', { type: 'image/png' })],
        configurable: true,
      });
      fileInput.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    // Verify re-add rendered the attachment.
    expect(el.textContent).toContain('test-screenshot.png');

    // Add text and send.
    const textarea = el.querySelector('textarea[placeholder="Type a message..."]') as HTMLTextAreaElement;
    expect(textarea).toBeTruthy();
    const nativeSetter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype, 'value',
    )!.set!;
    await act(async () => {
      nativeSetter.call(textarea, 'check this screenshot');
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
    });
    const sendBtn = el.querySelector('button[class*="bg-blue-600"]') as HTMLButtonElement;
    expect(sendBtn.disabled).toBe(false);
    await act(async () => { sendBtn.click(); });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 0)); });

    // Assert upload-URL endpoint was called.
    await waitFor(() => expect(uploadUrlCalls.length).toBe(1), { timeout: 3000 });
    expect(uploadUrlCalls[0].body).toMatchObject({ filename: 'test-screenshot.png', content_type: 'image/png' });

    // Assert GCS PUT upload was performed.
    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith('https://storage.googleapis.com/signed-upload', expect.anything());
    }, { timeout: 3000 });

    // Assert message POST carries the attachment with corrected type and signed download URL.
    await waitFor(() => expect(messagePostCalls.length).toBe(1), { timeout: 3000 });
    const sentPayload = messagePostCalls[0].body as Record<string, unknown>;
    const ctx = sentPayload.context;
    expect(ctx).toBeTruthy();
    const attachments = (ctx as Record<string, unknown>).attachments as Array<Record<string, unknown>>;
    expect(attachments).toBeTruthy();
    expect(attachments).toHaveLength(1);
    expect(attachments[0].type).toBe('image');
    expect(attachments[0].url).toBe('https://storage.googleapis.com/signed-download');
    expect(attachments[0].mime_type).toBe('image/png');
    expect(attachments[0].name).toBe('test-screenshot.png');

    globalThis.fetch = origFetch;
  });

  test('attachment upload flow: upload failure does not send message without attachment', async () => {
    const messagePostCalls: Array<{ body: unknown }> = [];

    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      const body = (init as RequestInit | undefined)?.body;
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (urlStr.includes('/attachment-upload-url') && method === 'POST') {
        return new Response(JSON.stringify({
          upload_url: 'https://storage.googleapis.com/signed-upload-fails',
          download_url: 'https://storage.googleapis.com/signed-download',
          storage_uri: 'gs://bucket/message-attachments/proj-1/att-123/test.png',
          attachment_id: 'att-123',
          size_bytes: JSON.parse(String(body)).size_bytes,
        }), { status: 200 });
      }
      if (/\/api\/admin\/messages$/.test(urlStr) && method === 'POST') {
        messagePostCalls.push({ body: body ? JSON.parse(String(body)) : null });
        return new Response(JSON.stringify({ id: 'm1', status: 'created', arming: 'steering' }), { status: 200 });
      }
      if (/\/api\/admin\/messages(\?|$)/.test(urlStr) && method === 'GET') {
        return new Response(JSON.stringify({ messages: [] }), { status: 200 });
      }
      return new Response('', { status: 404 });
    });

    const origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url) === 'https://storage.googleapis.com/signed-upload-fails') {
        return new Response(null, { status: 500 });
      }
      return origFetch(url);
    });

    try {
      const addToast = vi.fn();
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
            addToast={addToast as never}
            onMessageCountChange={() => {}}
          />,
        );
      });
      await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
      const el = host as HTMLDivElement;

      const fileInput = el.querySelector('input[type="file"]') as HTMLInputElement;
      expect(fileInput).toBeTruthy();
      await act(async () => {
        Object.defineProperty(fileInput, 'files', {
          value: [new File(['fake-png'], 'test-screenshot.png', { type: 'image/png' })],
          configurable: true,
        });
        fileInput.dispatchEvent(new Event('change', { bubbles: true }));
      });
      await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });

      const textarea = el.querySelector('textarea[placeholder="Type a message..."]') as HTMLTextAreaElement;
      expect(textarea).toBeTruthy();
      const nativeSetter = Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype, 'value',
      )!.set!;
      await act(async () => {
        nativeSetter.call(textarea, 'check this screenshot');
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
      });
      const sendBtn = el.querySelector('button[class*="bg-blue-600"]') as HTMLButtonElement;
      expect(sendBtn.disabled).toBe(false);
      await act(async () => { sendBtn.click(); });

      await waitFor(() => {
        expect(addToast).toHaveBeenCalledWith('Failed to upload test-screenshot.png', 'error');
      }, { timeout: 3000 });
      expect(addToast).not.toHaveBeenCalledWith('Failed to send message', 'error');
      expect(messagePostCalls).toHaveLength(0);
      expect(el.textContent).toContain('test-screenshot.png');
      expect((apiFetch as ReturnType<typeof vi.fn>).mock.calls.some(call => (
        String(call[0]).includes('/attachment-uploads/att-123')
        && (call[1] as RequestInit | undefined)?.method === 'DELETE'
      ))).toBe(true);
    } finally {
      globalThis.fetch = origFetch;
    }
  });

  test('refreshes strategic state once after duplicate steering replay', async () => {
    _setupSendMock({ arming: 'duplicate' });
    const addToast = vi.fn();
    const onStrategicRefresh = vi.fn();
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
          addToast={addToast as never}
          onMessageCountChange={() => {}}
          onStrategicRefresh={onStrategicRefresh}
        />,
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    const el = host as HTMLDivElement;

    await typeAndSend(el, 'steer: focus on the migration work');
    await waitFor(() => expect(addToast).toHaveBeenCalledTimes(1));
    expect(addToast).toHaveBeenCalledWith(armingToastCopy('duplicate'), 'success');
    // duplicate is steady-state narration: no inline composer hint for it.
    expect(hintText(el)).toBeNull();
    expect(onStrategicRefresh).toHaveBeenCalledTimes(1);
  });
});

// ── Attachment integration ─────────────────────────────────────────────────────
// Verifies that handleAttachFiles populates pendingAttachments (visible as
// preview chips), handleRemoveAttachment removes by index, and that submitted
// messages include context.attachments in the POST body.

describe('MessagesPanel attachment integration', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  let originalFetch: typeof globalThis.fetch | undefined;

  beforeEach(async () => {
    localStorage.clear();
    sessionStorage.clear();
    pendingAttachmentStore.resetPendingAttachmentMemoryStoreForTests();
    originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url) === 'https://storage.googleapis.com/signed-upload') {
        return new Response(null, { status: 200 });
      }
      return originalFetch
        ? originalFetch(url)
        : new Response('', { status: 404 });
    });
  });

  afterEach(() => {
    const r = root;
    if (r) act(() => r.unmount());
    root = null;
    host?.remove();
    host = null;
    if (originalFetch) globalThis.fetch = originalFetch;
    sessionStorage.clear();
    vi.clearAllMocks();
  });

  function _setupAttachmentMock(capturedBodies: Record<string, unknown>[]): void {
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (urlStr.includes('/attachment-upload-url') && method === 'POST') {
        return new Response(JSON.stringify({
          upload_url: 'https://storage.googleapis.com/signed-upload',
          download_url: 'https://storage.googleapis.com/signed-download',
          storage_uri: 'gs://bucket/message-attachments/proj-1/att-123/report.txt',
          attachment_id: 'att-123',
          size_bytes: JSON.parse(String((init as RequestInit).body)).size_bytes,
        }), { status: 200 });
      }
      if (/\/api\/admin\/messages(\?|$)/.test(urlStr) && method === 'POST') {
        capturedBodies.push(JSON.parse(String((init as RequestInit).body)));
        return new Response(JSON.stringify({ arming: 'steering' }), { status: 200 });
      }
      if (/\/api\/admin\/messages(\?|$)/.test(urlStr) && method === 'GET') {
        return new Response(JSON.stringify({ messages: [] }), { status: 200 });
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

  async function renderPanel(
    addToast: (msg: string, level: string) => void,
    props: Partial<MessagesPanelProps> = {},
  ) {
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
          addToast={addToast as never}
          onMessageCountChange={() => {}}
          {...props}
        />,
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    return host as HTMLDivElement;
  }

  async function attachFile(el: HTMLElement, name: string, content: string, mimeType: string): Promise<void> {
    const fileInput = el.querySelector<HTMLInputElement>('input[type="file"]')!;
    expect(fileInput).toBeTruthy();
    const file = new File([content], name, { type: mimeType });
    const dt = new DataTransfer();
    dt.items.add(file);
    // Use a writable data-property descriptor so the same input can be reused
    // across multiple calls in one test (re-defining is allowed when configurable).
    Object.defineProperty(fileInput, 'files', {
      value: dt.files,
      writable: true,
      configurable: true,
    });
    await act(async () => {
      fileInput.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await waitFor(() => expect((el.querySelector('button[aria-label="Attach file"]') as HTMLButtonElement).disabled).toBe(false));
  }

  test('handleAttachFiles renders attachment preview chips with file name', async () => {
    const addToast = vi.fn();
    _setupAttachmentMock([]);
    const el = await renderPanel(addToast);

    await attachFile(el, 'test.txt', 'Hello, world!', 'text/plain');

    await waitFor(() => {
      expect(el.textContent).toContain('test.txt');
    });
  });

  test('a downgrade while attachment upload is in flight performs no durable message write', async () => {
    let resolveUpload: (response: Response) => void = () => {};
    const uploadResponse = new Promise<Response>(resolve => { resolveUpload = resolve; });
    const fallbackFetch = globalThis.fetch;
    globalThis.fetch = vi.fn((url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url) === 'https://storage.googleapis.com/signed-upload') return uploadResponse;
      return fallbackFetch(url, init);
    }) as typeof globalThis.fetch;
    const durableMessageWrites: string[] = [];
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (urlStr.includes('/strategic-conversation') && method === 'GET') {
        return new Response(JSON.stringify({ conversation: { limit: 100, has_more: false, items: [] } }), { status: 200 });
      }
      if (urlStr.includes('/attachment-upload-url') && method === 'POST') {
        return new Response(JSON.stringify({
          upload_url: 'https://storage.googleapis.com/signed-upload',
          download_url: 'https://storage.googleapis.com/signed-download',
          storage_uri: 'gs://bucket/revoked-upload',
          attachment_id: 'revoked-upload',
          size_bytes: JSON.parse(String((init as RequestInit).body)).size_bytes,
        }), { status: 200 });
      }
      if ((urlStr.includes('/strategic-session/messages') || /\/api\/admin\/messages(?:\?|$)/.test(urlStr)) && method === 'POST') {
        durableMessageWrites.push(urlStr);
      }
      return new Response(JSON.stringify({ messages: [] }), { status: 200 });
    });
    const el = await renderPanel(vi.fn(), { filterChrome: 'minimal', canOperateProject: true });
    await attachFile(el, 'revoked.txt', 'private bytes', 'text/plain');
    fireEvent.change(el.querySelector('textarea')!, { target: { value: 'Do not commit after downgrade' } });
    fireEvent.click(el.querySelector('button[aria-label="Send message"]')!);
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledWith(
      'https://storage.googleapis.com/signed-upload',
      expect.anything(),
    ));

    await act(async () => {
      root!.render(
        <MessagesPanel
          selectedProjectId="proj-1"
          canOperateProject={false}
          filterChrome="minimal"
          onClose={() => {}}
          addToast={vi.fn() as never}
          onMessageCountChange={() => {}}
        />,
      );
    });
    await act(async () => {
      resolveUpload(new Response(null, { status: 200 }));
      await uploadResponse;
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(durableMessageWrites).toEqual([]);
  });

  test('failed add retains a pre-existing identity owned by text and reply context', async () => {
    const draftKey = 'messages_composer_draft:test-user@example.com:proj-1';
    const identityKey = 'conversationDraftSend:test-user%40example.com:proj-1';
    localStorage.setItem(draftKey, JSON.stringify({
      version: 2, actor_user_id: 'test-user@example.com', project_id: 'proj-1', type: 'FEEDBACK',
      content: 'Keep this draft', reply_to: { id: 'question-1', content: 'Which runner?', from: 'Strategic', message_type: 'QUESTION', status: 'open' },
      source_refs: [], artifact_refs: [], attachment_send_key: 'pre-existing-identity',
    }));
    _setupAttachmentMock([]);
    const el = await renderPanel(vi.fn(), { filterChrome: 'minimal' });
    await waitFor(() => expect(sessionStorage.getItem(identityKey)).toBe('pre-existing-identity'));
    const putSpy = vi.spyOn(pendingAttachmentStore, 'putPendingAttachment').mockRejectedValue(new Error('quota'));
    try {
      await attachFile(el, 'failed.txt', 'failed bytes', 'text/plain');
      expect(sessionStorage.getItem(identityKey)).toBe('pre-existing-identity');
      expect(JSON.parse(localStorage.getItem(draftKey) ?? '{}')).toMatchObject({
        content: 'Keep this draft', reply_to: { id: 'question-1' }, attachment_send_key: 'pre-existing-identity',
      });
    } finally {
      putSpy.mockRestore();
    }
  });

  test('attachment additions stay locked until the durable restore snapshot settles', async () => {
    localStorage.setItem('messages_composer_draft:test-user@example.com:proj-1', JSON.stringify({
      version: 2,
      actor_user_id: 'test-user@example.com',
      project_id: 'proj-1',
      type: 'FEEDBACK',
      content: '',
      reply_to: null,
      source_refs: [],
      artifact_refs: [],
      attachment_send_key: 'stale-restore-race',
    }));
    _setupAttachmentMock([]);
    let resolveRestore!: (items: never[]) => void;
    const staleRestore = new Promise<never[]>(resolve => { resolveRestore = resolve; });
    const listSpy = vi.spyOn(pendingAttachmentStore, 'listPendingAttachments').mockReturnValue(staleRestore);
    try {
      const el = await renderPanel(vi.fn(), { filterChrome: 'minimal' });
      await waitFor(() => expect(listSpy).toHaveBeenCalledTimes(1));
      const attach = el.querySelector<HTMLButtonElement>('button[aria-label="Attach file"]')!;
      expect(attach.disabled).toBe(true);

      resolveRestore([]);
      await act(async () => { await staleRestore; });
      await waitFor(() => expect(attach.disabled).toBe(false));

      const input = el.querySelector<HTMLInputElement>('input[type="file"]')!;
      const transfer = new DataTransfer();
      transfer.items.add(new File(['new durable bytes'], 'new-durable.txt', { type: 'text/plain' }));
      Object.defineProperty(input, 'files', { value: transfer.files, configurable: true });
      fireEvent.change(input);
      await waitFor(() => expect(el.textContent).toContain('new-durable.txt'));
    } finally {
      listSpy.mockRestore();
    }
  });

  test('send stays locked until durable draft attachments finish restoring', async () => {
    localStorage.setItem('messages_composer_draft:test-user@example.com:proj-1', JSON.stringify({
      version: 2,
      actor_user_id: 'test-user@example.com',
      project_id: 'proj-1',
      type: 'FEEDBACK',
      content: 'Send with the restored file',
      reply_to: null,
      source_refs: [],
      artifact_refs: [],
      attachment_send_key: 'restore-before-send',
    }));
    _setupAttachmentMock([]);
    type StoredAttachment = { id: string; file: File; mime_type: string; name: string; size_bytes: number };
    let resolveRestore!: (items: StoredAttachment[]) => void;
    const deferredRestore = new Promise<StoredAttachment[]>(resolve => { resolveRestore = resolve; });
    const listSpy = vi.spyOn(pendingAttachmentStore, 'listPendingAttachments').mockReturnValue(deferredRestore);
    try {
      const el = await renderPanel(vi.fn(), { filterChrome: 'minimal' });
      await waitFor(() => expect(listSpy).toHaveBeenCalled());
      const send = el.querySelector<HTMLButtonElement>('button[aria-label="Send message"]')!;
      expect(send.disabled).toBe(true);

      fireEvent.click(send);
      expect(vi.mocked(apiFetch).mock.calls.filter(call => (
        String(call[0]).includes('/strategic-session/messages')
        && (call[1] as RequestInit | undefined)?.method === 'POST'
      ))).toHaveLength(0);

      resolveRestore([{
        id: 'restored-file',
        file: new File(['restored bytes'], 'restored.txt', { type: 'text/plain' }),
        mime_type: 'text/plain',
        name: 'restored.txt',
        size_bytes: 14,
      }]);
      await waitFor(() => expect(el.textContent).toContain('restored.txt'));
      await waitFor(() => expect(send.disabled).toBe(false));
    } finally {
      listSpy.mockRestore();
    }
  });

  test('failed durable attachment restore keeps send locked while automatic retry recovers it', async () => {
    localStorage.setItem('messages_composer_draft:test-user@example.com:proj-1', JSON.stringify({
      version: 2,
      actor_user_id: 'test-user@example.com',
      project_id: 'proj-1',
      type: 'FEEDBACK',
      content: 'Do not send without the restored file',
      reply_to: null,
      source_refs: [],
      artifact_refs: [],
      attachment_send_key: 'retry-restore-before-send',
    }));
    _setupAttachmentMock([]);
    const addToast = vi.fn();
    const listSpy = vi.spyOn(pendingAttachmentStore, 'listPendingAttachments')
      .mockRejectedValueOnce(new Error('temporary IndexedDB failure'))
      .mockResolvedValue([{
        id: 'retried-file',
        file: new File(['retried bytes'], 'retried.txt', { type: 'text/plain' }),
        mime_type: 'text/plain',
        name: 'retried.txt',
        size_bytes: 13,
      }]);
    try {
      const el = await renderPanel(addToast, { filterChrome: 'minimal' });
      await waitFor(() => expect(listSpy).toHaveBeenCalledTimes(1));
      const send = el.querySelector<HTMLButtonElement>('button[aria-label="Send message"]')!;
      expect(send.disabled).toBe(true);
      expect(addToast).toHaveBeenCalledWith(
        'Could not restore pending attachments; retrying automatically',
        'error',
      );

      await waitFor(() => expect(listSpy).toHaveBeenCalledTimes(2));
      await waitFor(() => expect(el.textContent).toContain('retried.txt'));
      await waitFor(() => expect(send.disabled).toBe(false));
    } finally {
      listSpy.mockRestore();
    }
  });

  test('failed first add clears only actor A identity after switching to actor B in the same project', async () => {
    const mutableAuth = auth as unknown as { currentUser: { email: string; uid?: string } };
    mutableAuth.currentUser = { email: 'a@example.com', uid: 'actor-a' };
    localStorage.setItem('messages_composer_draft:actor-b:proj-1', JSON.stringify({
      version: 2, actor_user_id: 'actor-b', project_id: 'proj-1', type: 'FEEDBACK', content: 'actor B draft',
      reply_to: null, source_refs: [], artifact_refs: [], attachment_send_key: 'actor-b-identity',
    }));
    _setupAttachmentMock([]);
    let rejectAdd!: (error: Error) => void;
    const deferredFailure = new Promise<void>((_, reject) => { rejectAdd = reject; });
    const putSpy = vi.spyOn(pendingAttachmentStore, 'putPendingAttachment').mockReturnValue(deferredFailure);
    try {
      const el = await renderPanel(vi.fn(), { filterChrome: 'minimal' });
      const input = el.querySelector<HTMLInputElement>('input[type="file"]')!;
      const transfer = new DataTransfer();
      transfer.items.add(new File(['actor A bytes'], 'actor-a.txt', { type: 'text/plain' }));
      Object.defineProperty(input, 'files', { value: transfer.files, configurable: true });
      fireEvent.change(input);
      await waitFor(() => expect(putSpy).toHaveBeenCalledTimes(1));
      await waitFor(() => expect(sessionStorage.getItem('conversationDraftSend:actor-a:proj-1')).toBeTruthy());

      mutableAuth.currentUser = { email: 'b@example.com', uid: 'actor-b' };
      await act(async () => {
        root?.render(<MessagesPanel selectedProjectId="proj-1" addToast={() => {}} onMessageCountChange={() => {}} filterChrome="minimal" focusComposerSignal={1} />);
      });
      await waitFor(() => expect((el.querySelector('textarea') as HTMLTextAreaElement).value).toBe('actor B draft'));
      await waitFor(() => expect(sessionStorage.getItem('conversationDraftSend:actor-b:proj-1')).toBe('actor-b-identity'));

      rejectAdd(new Error('IndexedDB failed'));
      await waitFor(() => expect(sessionStorage.getItem('conversationDraftSend:actor-a:proj-1')).toBeNull());
      expect(sessionStorage.getItem('conversationDraftSend:actor-b:proj-1')).toBe('actor-b-identity');
      expect((el.querySelector('textarea') as HTMLTextAreaElement).value).toBe('actor B draft');
    } finally {
      mutableAuth.currentUser = { email: 'test-user@example.com' };
      putSpy.mockRestore();
    }
  });

  test('pending attachment bytes restore after a project switch before upload', async () => {
    const addToast = vi.fn();
    _setupAttachmentMock([]);
    const el = await renderPanel(addToast, { filterChrome: 'minimal' });
    await attachFile(el, 'durable.txt', 'durable attachment bytes', 'text/plain');
    await waitFor(() => expect(el.textContent).toContain('durable.txt'));

    _suppressSinceLastVisit('test-user@example.com', 'proj-2');
    await act(async () => {
      root?.render(
        <MessagesPanel
          selectedProjectId="proj-2"
          onClose={() => {}}
          addToast={addToast}
          onMessageCountChange={() => {}}
          filterChrome="minimal"
        />,
      );
    });
    await waitFor(() => expect(el.textContent).not.toContain('durable.txt'));

    await act(async () => {
      root?.render(
        <MessagesPanel
          selectedProjectId="proj-1"
          onClose={() => {}}
          addToast={addToast}
          onMessageCountChange={() => {}}
          filterChrome="minimal"
        />,
      );
    });
    await waitFor(() => expect(el.textContent).toContain('durable.txt'));
  });

  test('removing the final empty-draft attachment clears its durable send identity across remount', async () => {
    _setupAttachmentMock([]);
    const el = await renderPanel(vi.fn(), { filterChrome: 'minimal' });
    await attachFile(el, 'remove-me.txt', 'temporary bytes', 'text/plain');
    await waitFor(() => expect(el.textContent).toContain('remove-me.txt'));
    const identityKey = 'conversationDraftSend:test-user%40example.com:proj-1';
    await waitFor(() => expect(loadComposerDraft('test-user@example.com', 'proj-1')?.attachment_send_key).toBeTruthy());
    expect(sessionStorage.getItem(identityKey)).toBeTruthy();

    await act(async () => {
      (el.querySelector('button[aria-label="Remove remove-me.txt"]') as HTMLButtonElement).click();
    });
    await waitFor(() => expect(el.textContent).not.toContain('remove-me.txt'));
    await waitFor(() => expect(loadComposerDraft('test-user@example.com', 'proj-1')).toBeNull());
    expect(sessionStorage.getItem(identityKey)).toBeNull();

    await act(async () => { root?.render(<div data-testid="away" />); });
    await act(async () => {
      root?.render(
        <MessagesPanel
          selectedProjectId="proj-1"
          onClose={() => {}}
          addToast={() => {}}
          onMessageCountChange={() => {}}
          filterChrome="minimal"
        />,
      );
    });
    await waitFor(() => expect(el.querySelector('textarea')).toBeTruthy());
    expect(el.textContent).not.toContain('remove-me.txt');
    expect(loadComposerDraft('test-user@example.com', 'proj-1')).toBeNull();
    expect(sessionStorage.getItem(identityKey)).toBeNull();
  });

  test('deferred attachment deletion locks Send and prevents POST until removal settles', async () => {
    const messageBodies: Array<Record<string, unknown>> = [];
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (urlStr.includes('/strategic-conversation') && method === 'GET') {
        return new Response(JSON.stringify({ conversation: { limit: 100, has_more: false, items: [] } }), { status: 200 });
      }
      if (urlStr.includes('/strategic-session/messages') && method === 'POST') {
        const body = JSON.parse(String((init as RequestInit).body)) as Record<string, unknown>;
        messageBodies.push(body);
        return new Response(JSON.stringify({
          message: makeMsg({
            id: 'after-delete-message', project_id: 'proj-1', from_type: 'user',
            content: String(body.content), surface: 'conversation', handling_state: 'pending',
            idempotency_key: String(body.idempotency_key),
          }),
          wakeup_state: 'queued',
        }), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });
    let finishDelete!: () => void;
    const deletion = new Promise<void>(resolve => { finishDelete = resolve; });
    const deleteSpy = vi.spyOn(pendingAttachmentStore, 'deletePendingAttachment').mockReturnValue(deletion);
    try {
      const el = await renderPanel(vi.fn(), { filterChrome: 'minimal' });
      await attachFile(el, 'delete-before-send.txt', 'temporary bytes', 'text/plain');
      await waitFor(() => expect(el.textContent).toContain('delete-before-send.txt'));
      fireEvent.change(el.querySelector('textarea')!, { target: { value: 'Send only after deletion' } });
      fireEvent.click(el.querySelector('button[aria-label="Remove delete-before-send.txt"]')!);
      await waitFor(() => expect(deleteSpy).toHaveBeenCalledTimes(1));

      const send = el.querySelector('button[aria-label="Send message"]') as HTMLButtonElement;
      expect(send.disabled).toBe(true);
      fireEvent.click(send);
      expect(messageBodies).toHaveLength(0);

      finishDelete();
      await waitFor(() => expect(el.textContent).not.toContain('delete-before-send.txt'));
      await waitFor(() => expect(send.disabled).toBe(false));
      fireEvent.click(send);
      await waitFor(() => expect(messageBodies).toHaveLength(1));
    } finally {
      deleteSpy.mockRestore();
    }
  });

  test('deferred replacement excludes delete until the new bytes are durable', async () => {
    _setupAttachmentMock([]);
    const el = await renderPanel(vi.fn(), { filterChrome: 'minimal' });
    await attachFile(el, 'replace-race.png', 'old image bytes', 'image/png');
    await waitFor(() => expect(el.textContent).toContain('replace-race.png'));
    let finishReplace!: () => void;
    const replacement = new Promise<void>(resolve => { finishReplace = resolve; });
    const putSpy = vi.spyOn(pendingAttachmentStore, 'putPendingAttachment').mockReturnValue(replacement);
    putSpy.mockClear();
    const deleteSpy = vi.spyOn(pendingAttachmentStore, 'deletePendingAttachment').mockResolvedValue();
    try {
      fireEvent.click(el.querySelector('button[aria-label="Open replace-race.png preview"]')!);
      fireEvent.click(document.body.querySelector('button[aria-label="Replace image"]')!);
      const replaceInput = el.querySelector<HTMLInputElement>('input[accept="image/*"]')!;
      const transfer = new DataTransfer();
      transfer.items.add(new File(['new image bytes'], 'replacement.png', { type: 'image/png' }));
      Object.defineProperty(replaceInput, 'files', { value: transfer.files, configurable: true });
      fireEvent.change(replaceInput);
      await waitFor(() => expect(putSpy).toHaveBeenCalledTimes(1));

      const remove = el.querySelector('button[aria-label="Remove replace-race.png"]') as HTMLButtonElement;
      expect(remove.disabled).toBe(true);
      fireEvent.click(remove);
      expect(deleteSpy).not.toHaveBeenCalled();

      finishReplace();
      await waitFor(() => expect(el.textContent).toContain('replacement.png'));
      const settledRemove = el.querySelector('button[aria-label="Remove replacement.png"]') as HTMLButtonElement;
      await waitFor(() => expect(settledRemove.disabled).toBe(false));
      fireEvent.click(settledRemove);
      await waitFor(() => expect(deleteSpy).toHaveBeenCalledTimes(1));
    } finally {
      putSpy.mockRestore();
      deleteSpy.mockRestore();
    }
  });

  test('deferred replacement prevents Send until replacement bytes settle', async () => {
    const messageBodies: Array<Record<string, unknown>> = [];
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      if (urlStr.includes('/strategic-conversation') && method === 'GET') return new Response(JSON.stringify({ conversation: { limit: 100, has_more: false, items: [] } }), { status: 200 });
      if (urlStr.includes('/attachment-upload-url') && method === 'POST') {
        const size = JSON.parse(String((init as RequestInit).body)).size_bytes;
        return new Response(JSON.stringify({ upload_url: 'https://storage.googleapis.com/signed-upload', download_url: 'https://storage.googleapis.com/signed-download', storage_uri: 'gs://bucket/replacement', attachment_id: 'replacement-upload', size_bytes: size }), { status: 200 });
      }
      if (urlStr.includes('/strategic-session/messages') && method === 'POST') {
        const body = JSON.parse(String((init as RequestInit).body)) as Record<string, unknown>;
        messageBodies.push(body);
        return new Response(JSON.stringify({ message: makeMsg({ id: 'replacement-send', project_id: 'proj-1', from_type: 'user', content: String(body.content), surface: 'conversation', handling_state: 'pending', idempotency_key: String(body.idempotency_key) }), wakeup_state: 'queued' }), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });
    const el = await renderPanel(vi.fn(), { filterChrome: 'minimal' });
    await attachFile(el, 'replace-send.png', 'old image bytes', 'image/png');
    await waitFor(() => expect(el.textContent).toContain('replace-send.png'));
    let finishReplace!: () => void;
    const replacement = new Promise<void>(resolve => { finishReplace = resolve; });
    const putSpy = vi.spyOn(pendingAttachmentStore, 'putPendingAttachment').mockReturnValue(replacement);
    putSpy.mockClear();
    try {
      fireEvent.change(el.querySelector('textarea')!, { target: { value: 'Send replacement only' } });
      fireEvent.click(el.querySelector('button[aria-label="Open replace-send.png preview"]')!);
      fireEvent.click(document.body.querySelector('button[aria-label="Replace image"]')!);
      const replaceInput = el.querySelector<HTMLInputElement>('input[accept="image/*"]')!;
      const transfer = new DataTransfer();
      transfer.items.add(new File(['new image bytes'], 'send-ready.png', { type: 'image/png' }));
      Object.defineProperty(replaceInput, 'files', { value: transfer.files, configurable: true });
      fireEvent.change(replaceInput);
      await waitFor(() => expect(putSpy).toHaveBeenCalledTimes(1));
      const send = el.querySelector('button[aria-label="Send message"]') as HTMLButtonElement;
      expect(send.disabled).toBe(true);
      fireEvent.click(send);
      expect(messageBodies).toHaveLength(0);

      finishReplace();
      await waitFor(() => expect(el.textContent).toContain('send-ready.png'));
      await waitFor(() => expect(send.disabled).toBe(false));
      fireEvent.click(send);
      await waitFor(() => expect(messageBodies).toHaveLength(1));
      expect((messageBodies[0].attachments as Array<{ name: string }>)[0].name).toBe('send-ready.png');
    } finally {
      putSpy.mockRestore();
    }
  });

  test('handleRemoveAttachment removes attachment chip by index', async () => {
    const addToast = vi.fn();
    _setupAttachmentMock([]);
    const el = await renderPanel(addToast);

    // Attach two files
    await attachFile(el, 'first.txt', 'first', 'text/plain');
    await attachFile(el, 'second.txt', 'second', 'text/plain');

    await waitFor(() => {
      expect(el.textContent).toContain('first.txt');
      expect(el.textContent).toContain('second.txt');
    });

    // Remove the first attachment (index 0)
    const removeButtons = el.querySelectorAll<HTMLButtonElement>('button[aria-label^="Remove"]');
    expect(removeButtons.length).toBe(2);
    await act(async () => {
      removeButtons[0].click();
    });

    // The first file's name should be gone while second remains
    await waitFor(() => {
      expect(el.textContent).not.toContain('first.txt');
    });
    expect(el.textContent).toContain('second.txt');
  });

  test('sending with pendingAttachments uploads and includes signed download URL in POST body', async () => {
    const addToast = vi.fn();
    const capturedBodies: Record<string, unknown>[] = [];
    _setupAttachmentMock(capturedBodies);
    const el = await renderPanel(addToast);

    // Attach a file so pendingAttachments is non-empty
    await attachFile(el, 'report.txt', 'report content', 'text/plain');

    await waitFor(() => {
      expect(el.textContent).toContain('report.txt');
    });

    // Type and send a message (full chrome path: POST /api/admin/messages)
    const textarea = el.querySelector('textarea[placeholder="Type a message..."]') as HTMLTextAreaElement;
    expect(textarea).toBeTruthy();
    const nativeSetter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype, 'value',
    )!.set!;
    await act(async () => {
      nativeSetter.call(textarea, 'Here is the file');
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
    });
    const sendBtn = el.querySelector('button[class*="bg-blue-600"]') as HTMLButtonElement;
    expect(sendBtn).toBeTruthy();
    await act(async () => { sendBtn.click(); });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 0)); });

    // The POST body must include the uploaded attachment metadata and signed
    // download URL, not inline base64 data.
    expect(capturedBodies.length).toBeGreaterThanOrEqual(1);
    const postBody = capturedBodies[capturedBodies.length - 1];
    expect(postBody).toMatchObject({
      content: 'Here is the file',
      context: expect.objectContaining({
        attachments: expect.arrayContaining([
          expect.objectContaining({
            type: 'attachment',
            name: 'report.txt',
            mime_type: 'text/plain',
            size_bytes: 14,
            url: 'https://storage.googleapis.com/signed-download',
          }),
        ]),
      }),
    });
    const attachments = ((postBody.context as { attachments?: unknown[] }).attachments ?? []) as Array<Record<string, unknown>>;
    expect(attachments[0]).not.toHaveProperty('data');
  });

  test('oversized files (>50MB) are rejected with a toast and no chips', async () => {
    const addToast = vi.fn();
    _setupAttachmentMock([]);
    const el = await renderPanel(addToast);

    // Create a file that looks small but reports >50MB via size override
    const fileInput = el.querySelector<HTMLInputElement>('input[type="file"]')!;
    expect(fileInput).toBeTruthy();
    const file = new File(['tiny'], 'huge.mp4', { type: 'video/mp4' });
    Object.defineProperty(file, 'size', { value: 55 * 1024 * 1024 });
    const dt = new DataTransfer();
    dt.items.add(file);
    Object.defineProperty(fileInput, 'files', {
      value: dt.files,
      writable: true,
      configurable: true,
    });
    await act(async () => {
      fileInput.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 10)); });

    // The size-limit toast must fire
    expect(addToast).toHaveBeenCalledWith(
      expect.stringContaining('50 MB size limit'),
      'error',
    );
    // No attachment chips should appear (the file was rejected)
    expect(el.querySelector('[aria-label^="Remove"]')).toBeNull();
  });

  test('all files oversized triggers early return with no chips', async () => {
    const addToast = vi.fn();
    _setupAttachmentMock([]);
    const el = await renderPanel(addToast);

    // Two oversized files — both must be rejected
    const fileInput = el.querySelector<HTMLInputElement>('input[type="file"]')!;
    expect(fileInput).toBeTruthy();
    const file1 = new File(['x'], 'big1.bin', { type: 'application/octet-stream' });
    Object.defineProperty(file1, 'size', { value: 60 * 1024 * 1024 });
    const file2 = new File(['y'], 'big2.bin', { type: 'application/octet-stream' });
    Object.defineProperty(file2, 'size', { value: 70 * 1024 * 1024 });
    const dt = new DataTransfer();
    dt.items.add(file1);
    dt.items.add(file2);
    Object.defineProperty(fileInput, 'files', {
      value: dt.files,
      writable: true,
      configurable: true,
    });
    await act(async () => {
      fileInput.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 10)); });

    // One toast per rejected file; no file is retained for upload.
    expect(addToast).toHaveBeenCalledWith(
      expect.stringContaining('50 MB size limit'),
      'error',
    );
    expect(addToast).toHaveBeenCalledTimes(2);
    // No chips — early return before any file reading
    expect(el.querySelector('[aria-label^="Remove"]')).toBeNull();
  });

  test('image over ~9.75MB triggers a warning toast but is still accepted', async () => {
    const addToast = vi.fn();
    _setupAttachmentMock([]);
    const el = await renderPanel(addToast);

    const fileInput = el.querySelector<HTMLInputElement>('input[type="file"]')!;
    expect(fileInput).toBeTruthy();
    // 10 MB image — above the agent cap (~9.75 MB) but below the 50 MB hard limit
    const file = new File(['x'], 'large.png', { type: 'image/png' });
    Object.defineProperty(file, 'size', { value: 10 * 1024 * 1024 });
    const dt = new DataTransfer();
    dt.items.add(file);
    Object.defineProperty(fileInput, 'files', { value: dt.files, writable: true, configurable: true });
    await act(async () => {
      fileInput.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 10)); });

    // Warning toast must fire (not an error — file is accepted)
    expect(addToast).toHaveBeenCalledWith(
      expect.stringContaining('may not be visible to the strategist'),
      'info',
    );
    // File must still be accepted as an attachment chip
    await waitFor(() => {
      expect(el.querySelector('[aria-label^="Remove"]')).not.toBeNull();
    });
  });

  test('non-image file over ~9.75MB does NOT trigger the image warning toast', async () => {
    const addToast = vi.fn();
    _setupAttachmentMock([]);
    const el = await renderPanel(addToast);

    const fileInput = el.querySelector<HTMLInputElement>('input[type="file"]')!;
    expect(fileInput).toBeTruthy();
    // 10 MB PDF — above the agent image cap but not an image
    const file = new File(['x'], 'report.pdf', { type: 'application/pdf' });
    Object.defineProperty(file, 'size', { value: 10 * 1024 * 1024 });
    const dt = new DataTransfer();
    dt.items.add(file);
    Object.defineProperty(fileInput, 'files', { value: dt.files, writable: true, configurable: true });
    await act(async () => {
      fileInput.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 10)); });

    // No warning toast for non-image files
    expect(addToast).not.toHaveBeenCalledWith(
      expect.stringContaining('may not be visible to the strategist'),
      'info',
    );
    // File must still be accepted as an attachment chip
    await waitFor(() => {
      expect(el.querySelector('[aria-label^="Remove"]')).not.toBeNull();
    });
  });

  test('object URL preview failure still renders attachment chip', async () => {
    const addToast = vi.fn();
    _setupAttachmentMock([]);
    const el = await renderPanel(addToast);

    const originalCreateObjectURL = URL.createObjectURL;
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: vi.fn(() => { throw new Error('mock preview error'); }),
    });
    try {
      await attachFile(el, 'broken.png', 'content', 'image/png');

      await waitFor(() => {
        expect(el.textContent).toContain('broken.png');
      });
      expect(addToast).not.toHaveBeenCalledWith(
        expect.stringContaining('failed to read'),
        'error',
      );
    } finally {
      Object.defineProperty(URL, 'createObjectURL', {
        configurable: true,
        value: originalCreateObjectURL,
      });
    }
  });
});
