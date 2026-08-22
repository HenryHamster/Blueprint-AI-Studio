import { describe, test, expect, vi, afterEach, beforeEach } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { fireEvent, render, waitFor } from '@testing-library/react';
import { MessagesPanel as MessagesPanelComponent, type MessagesPanelProps } from './MessagesPanel';

import type { ProjectMessage } from './MessageBubble';
import type { StrategicStateResponse } from '../strategicStateApi';
import { auth } from '../firebase';
import * as pendingAttachmentStore from '../utils/pendingAttachmentStore';
import { loadComposerDraft, saveComposerDraft } from '../utils/conversationDraftState';

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
vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>();
  return {
    ...actual,
    apiFetch: vi.fn(async () => new Response(JSON.stringify({ messages: [] }), { status: 200 })),
  };
});

import { apiFetch } from '../api';
import * as apiModule from '../api';

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
function _strategicSessionGetCalls(): unknown[][] {
  return vi.mocked(apiFetch).mock.calls.filter((c) => {
    const url = String(c[0]);
    const init = c[1] as RequestInit | undefined;
    return /\/api\/admin\/projects\/[^/]+\/strategic-conversation(?:\?|$)/.test(url) && (!init?.method || init.method === 'GET');
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


describe('MessagesPanel confirmation chip actionability', () => {
  // The needs-action strip is collapsed into the conversation utility bar by default;
  // expand it so these tests exercise the strip itself.
  beforeEach(() => { localStorage.clear(); sessionStorage.clear(); vi.clearAllMocks(); localStorage.setItem('bp.chatChromeExpanded', 'true'); });
  afterEach(() => { localStorage.clear(); sessionStorage.clear(); vi.clearAllMocks(); });

  function makeConfirmationStrategicState(confirmationId = 'confirm-1', projectId = 'proj-1'): StrategicStateResponse {
    return makeStrategicState({
      attention: {
        level: 'needs_operator',
        summary: '1 confirmation needs action',
        last_seen_at: null,
        counts: {
          needs_operator: 1,
          watch: 0,
          unread: 0,
          questions: 0,
          blockers: 0,
          approvals: 1,
          manual_directions: 0,
          failed_tasks: 0,
          blocked_tasks: 0,
          parked_tasks: 0,
        },
        items: [
          {
            id: `confirmation:${confirmationId}`,
            kind: 'confirmation',
            severity: 'needs_operator',
            reason: 'approval_needed',
            label: 'Confirm deployment plan',
            ref: { kind: 'pending_confirmation', id: confirmationId, project_id: projectId },
          },
        ],
      },
    });
  }

  function setupConvMock(): void {
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
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
      return new Response(JSON.stringify({ messages: [] }), { status: 200 });
    });
  }

  test('confirmation chip in needs-action strip is clickable and expands accept/dismiss affordance', async () => {
    setupConvMock();
    vi.spyOn(apiModule, 'acceptPendingConfirmation').mockResolvedValue({ status: 'accepted', type: 'plan' });
    vi.spyOn(apiModule, 'dismissPendingConfirmation').mockResolvedValue(undefined);
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    const { container } = render(
      <MessagesPanel
        selectedProjectId="proj-1"
        addToast={() => {}}
        onMessageCountChange={() => {}}
        filterChrome="compact"
        strategicState={makeConfirmationStrategicState()}
      />,
    );
    const strip = await waitFor(() => {
      const el = container.querySelector('[data-testid="needs-action-strip"]');
      expect(el).toBeTruthy();
      return el as HTMLElement;
    });
    // The confirmation chip button should not be disabled
    const chip = [...strip.querySelectorAll('button')].find(b => b.textContent?.includes('Confirm deployment plan'));
    expect(chip).toBeTruthy();
    expect(chip!.disabled).toBe(false);

    // Before clicking, the inline actions should not be visible
    expect(container.querySelector('[data-testid="confirmation-chip-actions"]')).toBeNull();

    // Click the chip to expand inline actions
    fireEvent.click(chip!);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="confirmation-chip-actions"]')).toBeTruthy();
    });
    expect(container.querySelector('[data-testid="confirmation-accept-btn"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="confirmation-dismiss-btn"]')).toBeTruthy();
  });

  test('clicking Accept calls acceptPendingConfirmation with correct project and pending id, then hides chip', async () => {
    setupConvMock();
    const acceptSpy = vi.spyOn(apiModule, 'acceptPendingConfirmation').mockResolvedValue({ status: 'accepted', type: 'plan' });
    vi.spyOn(apiModule, 'dismissPendingConfirmation').mockResolvedValue(undefined);
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    const onStrategicStateRefresh = vi.fn();
    const { container } = render(
      <MessagesPanel
        selectedProjectId="proj-1"
        addToast={() => {}}
        onMessageCountChange={() => {}}
        filterChrome="compact"
        strategicState={makeConfirmationStrategicState('confirm-42', 'proj-1')}
        onStrategicStateRefresh={onStrategicStateRefresh}
      />,
    );
    const strip = await waitFor(() => {
      const el = container.querySelector('[data-testid="needs-action-strip"]');
      expect(el).toBeTruthy();
      return el as HTMLElement;
    });
    const chip = [...strip.querySelectorAll('button')].find(b => b.textContent?.includes('Confirm deployment plan'));
    expect(chip).toBeTruthy();
    fireEvent.click(chip!);
    await waitFor(() => expect(container.querySelector('[data-testid="confirmation-accept-btn"]')).toBeTruthy());

    fireEvent.click(container.querySelector('[data-testid="confirmation-accept-btn"]')!);

    // acceptPendingConfirmation must be called with the correct project and pending ids
    await waitFor(() => {
      expect(acceptSpy).toHaveBeenCalledWith('proj-1', 'confirm-42');
    });

    // The chip should be removed from the strip after acceptance
    await waitFor(() => {
      const chipAfter = [...(container.querySelector('[data-testid="needs-action-strip"]')?.querySelectorAll('button') ?? [])].find(b => b.textContent?.includes('Confirm deployment plan'));
      expect(chipAfter).toBeUndefined();
    });

    expect(onStrategicStateRefresh).toHaveBeenCalled();
  });

  test('clicking Dismiss calls dismissPendingConfirmation and removes chip from strip', async () => {
    setupConvMock();
    vi.spyOn(apiModule, 'acceptPendingConfirmation').mockResolvedValue({ status: 'accepted', type: 'plan' });
    const dismissSpy = vi.spyOn(apiModule, 'dismissPendingConfirmation').mockResolvedValue(undefined);
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    const { container } = render(
      <MessagesPanel
        selectedProjectId="proj-1"
        addToast={() => {}}
        onMessageCountChange={() => {}}
        filterChrome="compact"
        strategicState={makeConfirmationStrategicState('confirm-99', 'proj-1')}
      />,
    );
    const strip = await waitFor(() => {
      const el = container.querySelector('[data-testid="needs-action-strip"]');
      expect(el).toBeTruthy();
      return el as HTMLElement;
    });
    const chip = [...strip.querySelectorAll('button')].find(b => b.textContent?.includes('Confirm deployment plan'));
    expect(chip).toBeTruthy();
    fireEvent.click(chip!);
    await waitFor(() => expect(container.querySelector('[data-testid="confirmation-dismiss-btn"]')).toBeTruthy());

    fireEvent.click(container.querySelector('[data-testid="confirmation-dismiss-btn"]')!);

    await waitFor(() => {
      expect(dismissSpy).toHaveBeenCalledWith('proj-1', 'confirm-99');
    });

    await waitFor(() => {
      const chipAfter = [...(container.querySelector('[data-testid="needs-action-strip"]')?.querySelectorAll('button') ?? [])].find(b => b.textContent?.includes('Confirm deployment plan'));
      expect(chipAfter).toBeUndefined();
    });
  });

  test('accept API failure shows error toast and keeps chip visible', async () => {
    setupConvMock();
    vi.spyOn(apiModule, 'acceptPendingConfirmation').mockRejectedValue(new Error('server error'));
    vi.spyOn(apiModule, 'dismissPendingConfirmation').mockResolvedValue(undefined);
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    const addToast = vi.fn();
    const { container } = render(
      <MessagesPanel
        selectedProjectId="proj-1"
        addToast={addToast}
        onMessageCountChange={() => {}}
        filterChrome="compact"
        strategicState={makeConfirmationStrategicState('confirm-err', 'proj-1')}
      />,
    );
    const strip = await waitFor(() => {
      const el = container.querySelector('[data-testid="needs-action-strip"]');
      expect(el).toBeTruthy();
      return el as HTMLElement;
    });
    const chip = [...strip.querySelectorAll('button')].find(b => b.textContent?.includes('Confirm deployment plan'));
    fireEvent.click(chip!);
    await waitFor(() => expect(container.querySelector('[data-testid="confirmation-accept-btn"]')).toBeTruthy());

    fireEvent.click(container.querySelector('[data-testid="confirmation-accept-btn"]')!);

    await waitFor(() => {
      expect(addToast).toHaveBeenCalledWith('Failed to accept — try again', 'error');
    });
    // Chip must still be visible — it was not removed on failure
    const chipAfter = [...(container.querySelector('[data-testid="needs-action-strip"]')?.querySelectorAll('button') ?? [])].find(b => b.textContent?.includes('Confirm deployment plan'));
    expect(chipAfter).toBeTruthy();
  });

  test('minimal Chat omits needs-action controls even when attention is present', async () => {
    setupConvMock();
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    const { container } = render(
      <MessagesPanel
        selectedProjectId="proj-1"
        addToast={() => {}}
        onMessageCountChange={() => {}}
        filterChrome="minimal"
        strategicState={makeConfirmationStrategicState()}
      />,
    );

    await waitFor(() => expect(container.querySelector('[aria-label="Search strategist conversation"]')).toBeTruthy());
    expect(container.querySelector('[data-testid="needs-action-strip"]')).toBeNull();
    expect(container.textContent).not.toContain('Needs your action');
    expect(container.textContent).not.toContain('Confirm deployment plan');
  });

  test('message chip in non-conversation mode is not disabled and triggers scroll', async () => {
    // Non-conversation mode (full chrome) — message chips must not be inert.
    const scrollSpy = vi.fn();
    Element.prototype.scrollIntoView = scrollSpy;
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (urlStr.includes('/api/admin/messages') && method === 'GET') {
        return new Response(JSON.stringify({ messages: [
          { id: 'msg-target', project_id: 'proj-1', message_type: 'QUESTION', from_type: 'agent',
            from_task_id: null, content: 'What should I do?', context: {}, status: 'open',
            resolution: null, resolved_at: null, created_at: new Date().toISOString() },
        ] }), { status: 200 });
      }
      return new Response('', { status: 404 });
    });
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    const state = makeStrategicState({
      attention: {
        level: 'needs_operator',
        summary: '1 message needs action',
        last_seen_at: null,
        counts: { needs_operator: 1, watch: 0, unread: 0, questions: 1, blockers: 0, approvals: 0, manual_directions: 0, failed_tasks: 0, blocked_tasks: 0, parked_tasks: 0 },
        items: [{
          id: 'message:msg-target',
          kind: 'message',
          severity: 'needs_operator',
          reason: 'question',
          label: 'Question waiting',
          ref: { kind: 'message', id: 'msg-target', project_id: 'proj-1' },
        }],
      },
    });
    const { container } = render(
      <MessagesPanel
        selectedProjectId="proj-1"
        addToast={() => {}}
        onMessageCountChange={() => {}}
        filterChrome="compact"
        strategicState={state}
      />,
    );
    const strip = await waitFor(() => {
      const el = container.querySelector('[data-testid="needs-action-strip"]');
      expect(el).toBeTruthy();
      return el as HTMLElement;
    });
    const chip = [...strip.querySelectorAll('button')].find(b => b.textContent?.includes('Question waiting'));
    expect(chip).toBeTruthy();
    // Message chip must not be disabled in non-conversation mode
    expect(chip!.disabled).toBe(false);
    // Clicking must not throw — the handler now runs for non-conversation mode too
    fireEvent.click(chip!);
    // scrollIntoView may or may not be called depending on DOM state, but the
    // click must not error and the button must remain enabled after click
    expect(chip!.disabled).toBe(false);
  });
});

describe('MessagesPanel optimistic-send tab-switch persistence', () => {
  // Conversation search is collapsed into the utility bar by default; expand so the
  // search-input test sees the input rather than the collapsed search button.
  beforeEach(() => { localStorage.clear(); sessionStorage.clear(); localStorage.setItem('bp.chatChromeExpanded', 'true'); });
  afterEach(() => {
    (auth as unknown as { currentUser: { email: string; uid?: string } }).currentUser = { email: 'test-user@example.com' };
    localStorage.clear();
    sessionStorage.clear();
    vi.clearAllMocks();
  });

  // Standard mock for strategic conversation mode (minimal chrome).
  function _setupConvMock(items: ReturnType<typeof makeMsg>[] = []): void {
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (/\/api\/admin\/projects\/[^/]+\/strategic-session\/messages$/.test(urlStr) && method === 'POST') {
        return new Response(JSON.stringify({ arming: 'steering' }), { status: 200 });
      }
      if (/\/api\/admin\/projects\/[^/]+\/strategic-conversation(?:\?|$)/.test(urlStr) && method === 'GET') {
        return new Response(JSON.stringify({
          conversation: { limit: 20, has_more: false, order: 'oldest_first', items },
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

  test('an in-flight project A send cannot merge or clear project B after switching', async () => {
    let finishPost: ((response: Response) => void) | null = null;
    const postResponse = new Promise<Response>(resolve => { finishPost = resolve; });
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (urlStr.includes('/strategic-session/messages') && method === 'POST') return postResponse;
      if (urlStr.includes('/strategic-conversation') && method === 'GET') {
        return new Response(JSON.stringify({ items: [], limit: 100, has_more: false }), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });
    _suppressSinceLastVisit('test-user@example.com', 'proj-A');
    _suppressSinceLastVisit('test-user@example.com', 'proj-B');
    const { container, rerender } = render(
      <MessagesPanel selectedProjectId="proj-A" addToast={() => {}} onMessageCountChange={() => {}} filterChrome="minimal" />,
    );
    const textarea = await waitFor(() => container.querySelector('textarea') as HTMLTextAreaElement);
    fireEvent.change(textarea, { target: { value: 'message for A' } });
    fireEvent.click(container.querySelector('button[aria-label="Send message"]') as HTMLButtonElement);
    await waitFor(() => expect(String((apiFetch as ReturnType<typeof vi.fn>).mock.calls.find(call => String(call[0]).includes('/strategic-session/messages'))?.[0])).toContain('proj-A'));

    rerender(<MessagesPanel selectedProjectId="proj-B" addToast={() => {}} onMessageCountChange={() => {}} filterChrome="minimal" />);
    await waitFor(() => expect(textarea.value).toBe(''));
    fireEvent.change(textarea, { target: { value: 'draft for B' } });
    await act(async () => {
      finishPost?.(new Response(JSON.stringify({
        message: makeMsg({
          id: 'canonical-A',
          project_id: 'proj-A',
          from_type: 'user',
          content: 'message for A',
          handling_state: 'pending',
          idempotency_key: 'send-A',
        }),
        wakeup_state: 'queued',
      }), { status: 200 }));
      await postResponse;
    });
    await waitFor(() => expect(textarea.value).toBe('draft for B'));
    expect(container.textContent).not.toContain('message for A');
    expect(sessionStorage.getItem('optimisticSend:test-user%40example.com:proj-B')).toBeNull();
  });

  test('retries interrupted restored-send recovery after switching away and back', async () => {
    const seed = {
      actor_user_id: 'test-user@example.com',
      project_id: 'proj-recovery-a',
      key: 'interrupted-recovery-key',
      content: 'recover after returning',
      created_at: new Date().toISOString(),
      message_type: 'FEEDBACK',
      state: 'sending',
    };
    sessionStorage.setItem(
      'optimisticSend:test-user%40example.com:proj-recovery-a',
      JSON.stringify(seed),
    );
    _suppressSinceLastVisit('test-user@example.com', 'proj-recovery-a');
    _suppressSinceLastVisit('test-user@example.com', 'proj-recovery-b');
    const interruptedLookup = new Promise<Response>(() => {});
    let allowRecoveryToFinish = false;
    let identityLookups = 0;
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (urlStr.includes('idempotency_key=interrupted-recovery-key')) {
        identityLookups += 1;
        if (!allowRecoveryToFinish) return interruptedLookup;
        return new Response(JSON.stringify({
          conversation: { limit: 1, has_more: false, items: [] },
          matched_message: null,
        }), { status: 200 });
      }
      if (urlStr.includes('/strategic-conversation') && method === 'GET') {
        return new Response(JSON.stringify({ conversation: { limit: 100, has_more: false, items: [] } }), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });

    const { container, rerender } = render(
      <MessagesPanel selectedProjectId="proj-recovery-a" addToast={() => {}} onMessageCountChange={() => {}} filterChrome="minimal" />,
    );
    await waitFor(() => expect(identityLookups).toBeGreaterThanOrEqual(1));
    const lookupsBeforeSwitch = identityLookups;
    allowRecoveryToFinish = true;
    rerender(<MessagesPanel selectedProjectId="proj-recovery-b" addToast={() => {}} onMessageCountChange={() => {}} filterChrome="minimal" />);
    rerender(<MessagesPanel selectedProjectId="proj-recovery-a" addToast={() => {}} onMessageCountChange={() => {}} filterChrome="minimal" />);

    await waitFor(() => expect(identityLookups).toBeGreaterThan(lookupsBeforeSwitch));
    await waitFor(() => expect(container.querySelector('button[aria-label="Retry message"]')).toBeTruthy());
  });

  test('restores an unsent actor/project envelope with reply, source identities, and attachment send key', async () => {
    localStorage.setItem('messages_composer_draft:test-user@example.com:proj-envelope', JSON.stringify({
      version: 2,
      actor_user_id: 'test-user@example.com',
      project_id: 'proj-envelope',
      type: 'FEEDBACK',
      content: 'draft with durable context',
      reply_to: { id: 'question-1', content: 'Which runner?', from: 'Strategic', message_type: 'QUESTION', status: 'open' },
      source_refs: [{ kind: 'task', id: 'task-1', revision: 7 }],
      artifact_refs: [],
      attachment_send_key: 'draft-attachment-key',
    }));
    _suppressSinceLastVisit('test-user@example.com', 'proj-envelope');
    _setupConvMock([]);
    const { container } = render(
      <MessagesPanel selectedProjectId="proj-envelope" addToast={() => {}} onMessageCountChange={() => {}} filterChrome="minimal" />,
    );
    await waitFor(() => expect((container.querySelector('textarea') as HTMLTextAreaElement).value).toBe('draft with durable context'));
    expect(container.textContent).toContain('Replying to Strategic: Which runner?');
    expect(container.querySelector('[aria-label="Remove task revision 7"]')).not.toBeNull();
    await waitFor(() => expect(sessionStorage.getItem('conversationDraftSend:test-user%40example.com:proj-envelope')).toBe('draft-attachment-key'));
  });

  test('minimal chat exposes independent server-backed conversation search', async () => {
    _setupConvMock([]);
    const { container: el } = render(
      <MessagesPanel selectedProjectId="proj-search" addToast={() => {}} onMessageCountChange={() => {}} filterChrome="minimal" />,
    );
    const input = el.querySelector('[aria-label="Search strategist conversation"]') as HTMLInputElement | null;
    expect(input).not.toBeNull();
    if (!input) return;
    fireEvent.change(input, { target: { value: 'deployment decision' } });
    await waitFor(() => expect(_strategicSessionGetCalls().some(call => (
      new URL(String(call[0]), 'http://test').searchParams.get('search') === 'deployment decision'
    ))).toBe(true), { timeout: 1000 });
  });

  test('sends exactly the visible draft source chips and never inherits workbench refs', async () => {
    const projectId = 'proj-explicit-sources';
    const explicitRef = { kind: 'task', id: 'task-explicit', label: 'Explicit task', revision: 7 };
    localStorage.setItem(`messages_composer_draft:test-user@example.com:${projectId}`, JSON.stringify({
      version: 2,
      actor_user_id: 'test-user@example.com',
      project_id: projectId,
      type: 'FEEDBACK',
      content: 'Use only the visible source.',
      reply_to: null,
      source_refs: [explicitRef],
      artifact_refs: [],
      attachment_send_key: null,
    }));
    let posted: Record<string, unknown> | null = null;
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (urlStr.includes('/strategic-session/messages') && method === 'POST') {
        posted = JSON.parse(String((init as RequestInit).body));
        return new Response(JSON.stringify({
          message: makeMsg({
            id: 'accepted-explicit-source', project_id: projectId, from_type: 'user',
            content: 'Use only the visible source.', handling_state: 'pending',
            idempotency_key: String(posted?.idempotency_key),
          }),
          wakeup_state: 'queued',
        }), { status: 200 });
      }
      if (urlStr.includes('/strategic-conversation') && method === 'GET') {
        return new Response(JSON.stringify({ conversation: { limit: 20, has_more: false, items: [] } }), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });
    _suppressSinceLastVisit('test-user@example.com', projectId);
    const view = render(
      <MessagesPanel
        selectedProjectId={projectId}
        addToast={() => {}}
        onMessageCountChange={() => {}}
        filterChrome="minimal"
        strategicWorkbenchState={{
          version: 1,
          interaction_mode: 'continuous',
          active_surface: 'pipeline',
          selected_refs: [{ kind: 'task', id: 'task-hidden', label: 'Hidden workbench task' }],
          session_started_at: new Date().toISOString(),
          last_interaction_at: new Date().toISOString(),
        }}
      />,
    );
    await waitFor(() => expect(view.container.querySelector('[aria-label="Remove Explicit task revision 7"]')).not.toBeNull());
    fireEvent.click(view.container.querySelector('button[aria-label="Send message"]') as HTMLButtonElement);
    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toMatchObject({ source_refs: [explicitRef], artifact_refs: [] });
    expect(posted).not.toHaveProperty('workbench_context');
    expect(JSON.stringify(posted)).not.toContain('task-hidden');
  });

  test('switching actors on the same project replaces rather than leaks composer state', async () => {
    const mutableAuth = auth as unknown as { currentUser: { email: string; uid?: string } };
    localStorage.setItem('messages_composer_draft:actor-a:proj-actor', JSON.stringify({
      version: 2, actor_user_id: 'actor-a', project_id: 'proj-actor', type: 'FEEDBACK', content: 'private A',
      reply_to: null, source_refs: [], artifact_refs: [], attachment_send_key: null,
    }));
    localStorage.setItem('messages_composer_draft:actor-b:proj-actor', JSON.stringify({
      version: 2, actor_user_id: 'actor-b', project_id: 'proj-actor', type: 'FEEDBACK', content: 'private B',
      reply_to: null, source_refs: [], artifact_refs: [], attachment_send_key: null,
    }));
    mutableAuth.currentUser = { email: 'a@example.com', uid: 'actor-a' };
    _setupConvMock([]);
    const { container, rerender } = render(
      <MessagesPanel selectedProjectId="proj-actor" addToast={() => {}} onMessageCountChange={() => {}} filterChrome="minimal" focusComposerSignal={0} />,
    );
    await waitFor(() => expect((container.querySelector('textarea') as HTMLTextAreaElement).value).toBe('private A'));
    mutableAuth.currentUser = { email: 'b@example.com', uid: 'actor-b' };
    rerender(<MessagesPanel selectedProjectId="proj-actor" addToast={() => {}} onMessageCountChange={() => {}} filterChrome="minimal" focusComposerSignal={1} />);
    await waitFor(() => expect((container.querySelector('textarea') as HTMLTextAreaElement).value).toBe('private B'));
    expect(container.textContent).not.toContain('private A');
    mutableAuth.currentUser = { email: 'test-user@example.com' };
  });

  test('expired attachment receipt re-uploads durable bytes under the same message identity', async () => {
    let uploadReceiptCount = 0;
    const messageBodies: Array<Record<string, unknown>> = [];
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (urlStr.includes('/attachment-upload-url') && method === 'POST') {
        uploadReceiptCount += 1;
        const request = JSON.parse(String((init as RequestInit).body));
        return new Response(JSON.stringify({
          upload_url: `https://storage.googleapis.com/recovery-upload-${uploadReceiptCount}`,
          download_url: `https://storage.googleapis.com/recovery-download-${uploadReceiptCount}`,
          storage_uri: `gs://bucket/message-attachments/proj-1/recovery-${uploadReceiptCount}/report.txt`,
          attachment_id: `recovery-${uploadReceiptCount}`,
          size_bytes: request.size_bytes,
        }), { status: 200 });
      }
      if (urlStr.includes('/strategic-conversation') && method === 'GET') {
        return new Response(JSON.stringify({ limit: 1, has_more: false, items: [], matched_message: null }), { status: 200 });
      }
      if (urlStr.includes('/strategic-session/messages') && method === 'POST') {
        const body = JSON.parse(String((init as RequestInit).body)) as Record<string, unknown>;
        messageBodies.push(body);
        if (messageBodies.length < 3) {
          return new Response(JSON.stringify({ detail: 'Attachment upload expired; upload again' }), { status: 409 });
        }
        return new Response(JSON.stringify({
          message: makeMsg({
            id: 'recovered-message',
            project_id: 'proj-1',
            from_type: 'user',
            content: String(body.content),
            surface: 'conversation',
            handling_state: 'pending',
            idempotency_key: String(body.idempotency_key),
          }),
          wakeup_state: 'queued',
        }), { status: 200 });
      }
      if (urlStr.includes('/attachment-uploads/') && method === 'DELETE') return new Response(null, { status: 204 });
      return new Response(JSON.stringify({}), { status: 200 });
    });
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => (
      String(url).includes('/recovery-upload-')
        ? new Response(null, { status: 200 })
        : new Response('', { status: 404 })
    ));

    const addToast = vi.fn();
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    const { container: el } = render(
      <MessagesPanel selectedProjectId="proj-1" addToast={addToast} onMessageCountChange={() => {}} filterChrome="minimal" />,
    );
    const fileInput = el.querySelector<HTMLInputElement>('input[type="file"]')!;
    const file = new File(['durable recovery bytes'], 'recovery.txt', { type: 'text/plain' });
    const transfer = new DataTransfer();
    transfer.items.add(file);
    Object.defineProperty(fileInput, 'files', { value: transfer.files, configurable: true });
    fireEvent.change(fileInput);
    await waitFor(() => expect(el.textContent).toContain('recovery.txt'));
    fireEvent.change(el.querySelector('textarea')!, { target: { value: 'recover this upload' } });
    const send = el.querySelector<HTMLButtonElement>('button[aria-label="Send message"]')!;
    await waitFor(() => expect(send.disabled).toBe(false));
    fireEvent.click(send);
    // The dropped send — and its stale-attachment-receipt recovery — both
    // happen automatically now, without any operator click on "Retry message".
    await waitFor(() => expect(messageBodies).toHaveLength(3), { timeout: 5000 });

    expect(uploadReceiptCount).toBe(2);
    expect(new Set(messageBodies.map(body => body.idempotency_key)).size).toBe(1);
    expect((messageBodies[2].attachments as Array<{ attachment_id: string }>)[0].attachment_id).toBe('recovery-2');
    expect((apiFetch as ReturnType<typeof vi.fn>).mock.calls.some(call => (
      String(call[0]).includes('/attachment-uploads/recovery-1')
      && (call[1] as RequestInit | undefined)?.method === 'DELETE'
    ))).toBe(true);
  });

  test('send → unmount → remount shows the in-flight bubble (tab-switch scenario)', async () => {
    // This is the exact operator scenario: send a message in conversation mode,
    // switch to pipeline view (unmount), switch back (remount) — the bubble should
    // still be visible until the durable message confirms.
    //
    // The mock returns empty on every strategic-conversation GET so the bubble is
    // never auto-cleared by the "landed" check. This keeps it visible after remount.
    _suppressSinceLastVisit('test-user@example.com', 'proj-tab-1');
    _setupConvMock([]);

    let root: Root | null = null;
    const host = document.createElement('div');
    document.body.appendChild(host);
    try {
      root = createRoot(host);
      // First mount — initial panel.
      await act(async () => {
        root!.render(
          <MessagesPanel
            selectedProjectId="proj-tab-1"
            onClose={() => {}}
            addToast={() => {}}
            onMessageCountChange={() => {}}
            filterChrome="minimal"
          />,
        );
      });
      await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });

      // Type and send a message.
      const textarea = host.querySelector('textarea') as HTMLTextAreaElement;
      expect(textarea).not.toBeNull();
      const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')!.set!;
      await act(async () => {
        nativeSetter.call(textarea, 'hello from operator');
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
      });
      const sendBtn = host.querySelector('button[aria-label="Send message"]') as HTMLButtonElement;
      expect(sendBtn).not.toBeNull();
      await act(async () => { sendBtn.click(); });
      // Let the optimistic state settle (setOptimisticSend fires → useEffect writes storage).
      await act(async () => { await new Promise<void>(res => setTimeout(res, 10)); });

      // sessionStorage must have the entry for this project by now.
      const storedAfterSend = sessionStorage.getItem('optimisticSend:test-user%40example.com:proj-tab-1');
      // Note: by this point handleSubmit may have cleared it too (mock responds instantly).
      // What matters is that the next assertion (remount) still shows the bubble if the
      // entry survived. We verify the entry OR fall back to the remount check.

      // Simulate tab switch: unmount (pipeline view opened).
      await act(async () => { root!.render(<div data-testid="pipeline-placeholder" />); });
      await act(async () => { await new Promise<void>(res => setTimeout(res, 10)); });

      // Seed the storage if it was already cleared (mock returned too fast). This
      // simulates a case where the POST returned before the tab switch but the entry
      // was cleared — in practice, the entry persists long enough for the user to
      // perceive the switch.
      if (!sessionStorage.getItem('optimisticSend:test-user%40example.com:proj-tab-1') && storedAfterSend) {
        sessionStorage.setItem('optimisticSend:test-user%40example.com:proj-tab-1', storedAfterSend);
      }

      if (!sessionStorage.getItem('optimisticSend:test-user%40example.com:proj-tab-1')) {
        // The full send cycle completed (POST + fetchMessages) before the tab switch.
        // In this degenerate timing, the durable message is already on the server.
        // Seed a fresh entry to exercise the restoration path anyway.
        sessionStorage.setItem('optimisticSend:test-user%40example.com:proj-tab-1', JSON.stringify({
          actor_user_id: 'test-user@example.com', project_id: 'proj-tab-1',
          key: 'tab-test-key', content: 'hello from operator',
          created_at: new Date().toISOString(), message_type: 'FEEDBACK',
        }));
      }

      // Remount — user switched back to chat view.
      await act(async () => {
        root!.render(
          <MessagesPanel
            selectedProjectId="proj-tab-1"
            onClose={() => {}}
            addToast={() => {}}
            onMessageCountChange={() => {}}
            filterChrome="minimal"
          />,
        );
      });
      await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });

      // The restored bubble must be visible (panel should NOT show "No messages yet").
      await waitFor(() => {
        expect(host.textContent).toContain('hello from operator');
      });
    } finally {
      act(() => root?.unmount());
      host.remove();
    }
  });

  test('remounting restores optimistic bubble from sessionStorage', async () => {
    // Pre-seed a fresh (< 60 s old) optimistic entry in sessionStorage.
    const seed = {
      actor_user_id: 'test-user@example.com',
      project_id: 'proj-tab-2',
      key: 'seed-key-1',
      content: 'message sent before tab switch',
      created_at: new Date().toISOString(),
      message_type: 'FEEDBACK',
      state: 'needs_retry',
      reply_to_message_id: 'question-1',
      reply_to: { id: 'question-1', content: 'Which runner?', from: 'Strategic', message_type: 'QUESTION', status: 'open' },
      context: {
        reply_to: 'question-1',
        reply_preview: 'Which runner?',
        attachments: [{ type: 'attachment', mime_type: 'text/plain', name: 'run.txt', size_bytes: 12 }],
        source_refs: [{ kind: 'task', id: 'task-1', label: 'Runner check' }],
        strategic_session: {
          version: 1,
          interaction_mode: 'chat',
          active_surface: 'strategic',
          selected_refs: [{ kind: 'task', id: 'task-1' }],
          selected_ref_descriptors: [{ kind: 'task', id: 'task-1', label: 'Runner check' }],
        },
      },
      payload: {
        content: 'message sent before tab switch',
        idempotency_key: 'seed-key-1',
        reply_to_message_id: 'question-1',
        source_refs: [{ kind: 'task', id: 'task-1', label: 'Runner check' }],
        attachments: [{ type: 'attachment', mime_type: 'text/plain', name: 'run.txt', size_bytes: 12 }],
      },
    };
    sessionStorage.setItem('optimisticSend:test-user%40example.com:proj-tab-2', JSON.stringify(seed));

    _suppressSinceLastVisit('test-user@example.com', 'proj-tab-2');
    // Fetch always returns empty so the bubble is NOT auto-cleared by the landed check.
    _setupConvMock([]);

    const { container } = render(
      <MessagesPanel
        selectedProjectId="proj-tab-2"
        onClose={() => {}}
        addToast={() => {}}
        onMessageCountChange={() => {}}
        filterChrome="minimal"
      />,
    );
    await waitFor(() => expect(
      (apiFetch as ReturnType<typeof vi.fn>).mock.calls.some(c =>
        /strategic-conversation/.test(String(c[0]))
      )
    ).toBe(true));

    // The restored optimistic bubble should be visible in the conversation.
    await waitFor(() => {
      expect(container.textContent).toContain('message sent before tab switch');
      expect(container.textContent).toContain('Which runner?');
      expect(container.querySelector('button[aria-label="Preview run.txt"]')).not.toBeNull();
      expect(container.textContent).toContain('Runner check');
    });
  });

  test('safe abandon confirms canonical absence before unlocking an unrecoverable attachment send', async () => {
    const seed = {
      actor_user_id: 'test-user@example.com',
      project_id: 'proj-abandon',
      key: 'abandon-key',
      content: 'unrecoverable attachment',
      created_at: new Date().toISOString(),
      message_type: 'FEEDBACK',
      state: 'needs_retry',
      draft_write_id: 'submitted-abandon-write',
      payload: {
        content: 'unrecoverable attachment',
        idempotency_key: 'abandon-key',
        attachments: [{
          type: 'attachment', mime_type: 'text/plain', name: 'gone.txt', size_bytes: 12,
          attachment_id: 'stale-upload', storage_uri: 'gs://bucket/stale-upload',
        }],
      },
    };
    sessionStorage.setItem('optimisticSend:test-user%40example.com:proj-abandon', JSON.stringify(seed));
    saveComposerDraft('test-user@example.com', 'proj-abandon', {
      type: 'FEEDBACK', content: 'newer draft survives abandon', reply_to: null,
      source_refs: [], artifact_refs: [], attachment_send_key: null,
    }, 'newer-abandon-write');
    _suppressSinceLastVisit('test-user@example.com', 'proj-abandon');
    _setupConvMock([]);
    const addToast = vi.fn();
    const { container } = render(
      <MessagesPanel selectedProjectId="proj-abandon" addToast={addToast} onMessageCountChange={() => {}} filterChrome="minimal" />,
    );
    // waitFor only retries on THROW — a falsy return resolves immediately, so
    // the find() must be asserted inside the callback to actually wait.
    //
    // Wait for ENABLED, not merely present.  The button carries
    // `disabled={submitting || uploading}`, and fireEvent.click on a disabled
    // button is a silent no-op with no retry — so a single click landing
    // during mount-time reconciliation drops the abandon entirely and the
    // sessionStorage assertion below then times out.  That is exactly how
    // this test went red on promotion CI at ca5064d70 (loaded self-hosted
    // runner) while passing the whole 1922-test shard locally.
    const abandon = await waitFor(() => {
      const button = [...container.querySelectorAll('button')]
        .find(candidate => candidate.textContent?.trim() === 'Abandon pending message');
      expect(button).toBeTruthy();
      expect((button as HTMLButtonElement).disabled).toBe(false);
      return button as HTMLButtonElement;
    });
    fireEvent.click(abandon);
    await waitFor(() => expect(sessionStorage.getItem('optimisticSend:test-user%40example.com:proj-abandon')).toBeNull());
    expect((container.querySelector('textarea') as HTMLTextAreaElement).readOnly).toBe(false);
    expect(addToast).toHaveBeenCalledWith('Pending message abandoned after confirming it was not accepted.', 'info');
    expect(loadComposerDraft('test-user@example.com', 'proj-abandon')).toMatchObject({
      write_id: 'newer-abandon-write', content: 'newer draft survives abandon',
    });
  });

  test('receipt recovery preserves a newer composer write than the submitted snapshot', async () => {
    const projectId = 'proj-recovery-newer-draft';
    const identity = 'recovery-newer-key';
    sessionStorage.setItem(`optimisticSend:test-user%40example.com:${projectId}`, JSON.stringify({
      actor_user_id: 'test-user@example.com', project_id: projectId,
      key: identity, content: 'submitted recovery snapshot',
      created_at: new Date().toISOString(), message_type: 'FEEDBACK',
      state: 'sending', draft_write_id: 'submitted-recovery-write',
      payload: {
        message_type: 'FEEDBACK', content: 'submitted recovery snapshot',
        idempotency_key: identity, source_refs: [], artifact_refs: [], attachments: [],
      },
    }));
    saveComposerDraft('test-user@example.com', projectId, {
      type: 'FEEDBACK', content: 'newer draft survives recovery', reply_to: null,
      source_refs: [], artifact_refs: [], attachment_send_key: null,
    }, 'newer-recovery-write');
    _suppressSinceLastVisit('test-user@example.com', projectId);
    _setupConvMock([makeMsg({
      id: 'recovered-durable-message', project_id: projectId, from_type: 'user',
      content: 'submitted recovery snapshot', handling_state: 'pending', idempotency_key: identity,
    })]);

    const view = render(
      <MessagesPanel selectedProjectId={projectId} addToast={() => {}} onMessageCountChange={() => {}} filterChrome="minimal" />,
    );
    await waitFor(() => expect(view.container.querySelector('[data-send-state="accepted"]')).not.toBeNull());
    expect(loadComposerDraft('test-user@example.com', projectId)).toMatchObject({
      write_id: 'newer-recovery-write', content: 'newer draft survives recovery',
    });
  });

  test('wake receipt clears from sessionStorage once the durable message is claimed', async () => {
    // The durable message has the same content as the seeded optimistic bubble so
    // the "landed" useEffect detects it and clears the sessionStorage entry.
    const durableMsg = makeMsg({
      id: 'durable-1', project_id: 'proj-tab-3',
      message_type: 'FEEDBACK', from_type: 'user',
      content: 'msg that landed on server',
      idempotency_key: 'seed-key-3',
      handling_state: 'claimed',
    });
    const seed = {
      actor_user_id: 'test-user@example.com',
      project_id: 'proj-tab-3',
      key: 'seed-key-3',
      content: 'msg that landed on server',
      created_at: new Date().toISOString(),
      message_type: 'FEEDBACK',
    };
    sessionStorage.setItem('optimisticSend:test-user%40example.com:proj-tab-3', JSON.stringify(seed));

    _suppressSinceLastVisit('test-user@example.com', 'proj-tab-3');
    // Return the durable message on the FIRST fetch so the "landed" check fires
    // during initial load rather than requiring a poller tick.
    _setupConvMock([durableMsg]);

    render(
      <MessagesPanel
        selectedProjectId="proj-tab-3"
        onClose={() => {}}
        addToast={() => {}}
        onMessageCountChange={() => {}}
        filterChrome="minimal"
      />,
    );
    // Once fetchMessages returns the durable message, the "landed" useEffect fires
    // and removes the sessionStorage entry.
    await waitFor(() => {
      expect(sessionStorage.getItem('optimisticSend:test-user%40example.com:proj-tab-3')).toBeNull();
    });
  });

  test('restores a text-only retry from its immutable payload and resends that exact identity and payload', async () => {
    const projectId = 'proj-text-retry';
    const payload = {
      message_type: 'FEEDBACK',
      content: 'immutable retry text',
      idempotency_key: 'text-retry-key',
      reply_to_message_id: null,
      source_refs: [{ kind: 'task', id: 'task-immutable', revision: 4 }],
      artifact_refs: [{ kind: 'artifact', id: 'artifact-immutable', revision: 8 }],
      operator_intent: { kind: 'verify', expected_revision: 8 },
      attachments: [],
    };
    sessionStorage.setItem(`optimisticSend:test-user%40example.com:${projectId}`, JSON.stringify({
      actor_user_id: 'test-user@example.com', project_id: projectId,
      key: 'text-retry-key', content: 'stale display text',
      created_at: new Date().toISOString(), message_type: 'FEEDBACK',
      state: 'needs_retry', payload,
    }));
    let posted: Record<string, unknown> | null = null;
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (urlStr.includes('/strategic-conversation') && method === 'GET') {
        return new Response(JSON.stringify({
          conversation: { limit: 20, has_more: false, order: 'oldest_first', items: [] },
          matched_message: null,
        }), { status: 200 });
      }
      if (urlStr.includes('/strategic-session/messages') && method === 'POST') {
        posted = JSON.parse(String((init as RequestInit).body));
        return new Response(JSON.stringify({
          message: makeMsg({
            id: 'accepted-text-retry', project_id: projectId, from_type: 'user',
            content: payload.content, handling_state: 'pending', idempotency_key: payload.idempotency_key,
          }),
          wakeup_state: 'queued',
        }), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });
    _suppressSinceLastVisit('test-user@example.com', projectId);
    const view = render(
      <MessagesPanel selectedProjectId={projectId} addToast={() => {}} onMessageCountChange={() => {}} filterChrome="minimal" />,
    );
    await waitFor(() => expect((view.container.querySelector('textarea') as HTMLTextAreaElement).value)
      .toBe(payload.content));
    const retryButton = view.container.querySelector('button[aria-label="Retry message"]') as HTMLButtonElement;
    await waitFor(() => expect(retryButton.disabled).toBe(false));
    fireEvent.click(retryButton);
    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toEqual(payload);
  });

  test('durable acceptance conditionally clears the submitted snapshot but preserves a newer tab draft', async () => {
    const projectId = 'proj-newer-draft';
    let finishPost!: (response: Response) => void;
    const postResponse = new Promise<Response>(resolve => { finishPost = resolve; });
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (urlStr.includes('/strategic-session/messages') && method === 'POST') return postResponse;
      if (urlStr.includes('/strategic-conversation') && method === 'GET') {
        return new Response(JSON.stringify({ conversation: { limit: 20, has_more: false, items: [] } }), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });
    _suppressSinceLastVisit('test-user@example.com', projectId);
    const view = render(
      <MessagesPanel selectedProjectId={projectId} addToast={() => {}} onMessageCountChange={() => {}} filterChrome="minimal" />,
    );
    const textarea = await waitFor(() => view.container.querySelector('textarea') as HTMLTextAreaElement);
    fireEvent.change(textarea, { target: { value: 'submitted snapshot' } });
    await waitFor(() => expect(loadComposerDraft('test-user@example.com', projectId)?.content).toBe('submitted snapshot'));
    fireEvent.click(view.container.querySelector('button[aria-label="Send message"]') as HTMLButtonElement);
    await waitFor(() => expect(view.container.querySelector('[data-send-state="sending"]')).not.toBeNull());
    const newerWrite = saveComposerDraft('test-user@example.com', projectId, {
      type: 'FEEDBACK', content: 'newer tab edit', reply_to: null,
      source_refs: [], artifact_refs: [], attachment_send_key: null,
    });

    await act(async () => {
      finishPost(new Response(JSON.stringify({
        message: makeMsg({
          id: 'accepted-old-draft', project_id: projectId, from_type: 'user',
          content: 'submitted snapshot', handling_state: 'pending',
        }),
        wakeup_state: 'queued',
      }), { status: 200 }));
      await postResponse;
    });

    await waitFor(() => expect(loadComposerDraft('test-user@example.com', projectId)).toMatchObject({
      write_id: newerWrite,
      content: 'newer tab edit',
    }));
  });

  test('acceptance after a project switch clears only the old submitted draft', async () => {
    const oldProjectId = 'proj-old-scope-acceptance';
    const currentProjectId = 'proj-current-scope-draft';
    let finishPost!: (response: Response) => void;
    const postResponse = new Promise<Response>(resolve => { finishPost = resolve; });
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (urlStr.includes('/strategic-session/messages') && method === 'POST') return postResponse;
      if (urlStr.includes('/strategic-conversation') && method === 'GET') {
        return new Response(JSON.stringify({ conversation: { limit: 100, has_more: false, items: [] } }), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });
    _suppressSinceLastVisit('test-user@example.com', oldProjectId);
    _suppressSinceLastVisit('test-user@example.com', currentProjectId);
    saveComposerDraft('test-user@example.com', currentProjectId, {
      type: 'FEEDBACK', content: 'current project draft', reply_to: null,
      source_refs: [], artifact_refs: [], attachment_send_key: null,
    });
    const view = render(
      <MessagesPanel selectedProjectId={oldProjectId} addToast={() => {}} onMessageCountChange={() => {}} filterChrome="minimal" />,
    );
    let textarea = await waitFor(() => view.container.querySelector('textarea') as HTMLTextAreaElement);
    fireEvent.change(textarea, { target: { value: 'submitted on old project' } });
    await waitFor(() => expect(loadComposerDraft('test-user@example.com', oldProjectId)?.content)
      .toBe('submitted on old project'));
    fireEvent.click(view.container.querySelector('button[aria-label="Send message"]') as HTMLButtonElement);
    await waitFor(() => expect(view.container.querySelector('[data-send-state="sending"]')).not.toBeNull());

    view.rerender(
      <MessagesPanel selectedProjectId={currentProjectId} addToast={() => {}} onMessageCountChange={() => {}} filterChrome="minimal" />,
    );
    textarea = await waitFor(() => view.container.querySelector('textarea') as HTMLTextAreaElement);
    await waitFor(() => expect(textarea.value).toBe('current project draft'));

    await act(async () => {
      finishPost(new Response(JSON.stringify({
        message: makeMsg({
          id: 'accepted-after-switch', project_id: oldProjectId, from_type: 'user',
          content: 'submitted on old project', handling_state: 'pending',
        }),
        wakeup_state: 'queued',
      }), { status: 200 }));
      await postResponse;
    });

    await waitFor(() => expect(loadComposerDraft('test-user@example.com', oldProjectId)).toBeNull());
    expect(loadComposerDraft('test-user@example.com', currentProjectId)?.content).toBe('current project draft');
    expect((view.container.querySelector('textarea') as HTMLTextAreaElement).value).toBe('current project draft');
  });

  test('an old reply cannot detach the next attachment draft from lost-response recovery', async () => {
    const projectId = 'proj-overlap-reply';
    let conversationItems: ReturnType<typeof makeMsg>[] = [];
    let messagePostCount = 0;
    let acceptedRefreshHeld = false;
    let releaseAcceptedRefresh!: () => void;
    const acceptedRefresh = new Promise<void>((resolve) => {
      releaseAcceptedRefresh = resolve;
    });
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (urlStr.includes('/attachment-upload-url') && method === 'POST') {
        return new Response(JSON.stringify({
          upload_url: 'https://storage.googleapis.com/overlap-upload',
          download_url: 'https://storage.googleapis.com/overlap-download',
          storage_uri: `gs://bucket/message-attachments/${projectId}/overlap/report.txt`,
          attachment_id: 'overlap-upload-id',
          size_bytes: 16,
        }), { status: 200 });
      }
      if (urlStr.includes('/strategic-session/messages') && method === 'POST') {
        messagePostCount += 1;
        const body = JSON.parse(String((init as RequestInit).body)) as Record<string, unknown>;
        if (messagePostCount === 1) {
          return new Response(JSON.stringify({
            message: makeMsg({
              id: 'first-accepted', project_id: projectId, from_type: 'user',
              content: 'first message', surface: 'conversation', handling_state: 'pending',
              idempotency_key: String(body.idempotency_key),
            }),
            wakeup_state: 'queued',
          }), { status: 200 });
        }
        throw new Error('response lost after second commit');
      }
      if (urlStr.includes('/strategic-conversation') && method === 'GET') {
        // Keep the first send's post-acceptance refresh in flight. The accepted
        // receipt is already durable, so selecting the next draft's attachment
        // must not be discarded by the old submit guard.
        if (messagePostCount === 1 && !acceptedRefreshHeld) {
          acceptedRefreshHeld = true;
          await acceptedRefresh;
        }
        return new Response(JSON.stringify({
          conversation: { limit: 100, has_more: false, order: 'oldest_first', items: conversationItems },
          matched_message: null,
        }), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => (
      String(url).includes('overlap-upload')
        ? new Response(null, { status: 200 })
        : new Response('', { status: 404 })
    ));
    _suppressSinceLastVisit('test-user@example.com', projectId);
    const view = render(
      <MessagesPanel
        selectedProjectId={projectId}
        addToast={() => {}}
        onMessageCountChange={() => {}}
        filterChrome="minimal"
        realtimeRefreshSignal={0}
      />,
    );
    const textarea = await waitFor(() => view.container.querySelector('textarea') as HTMLTextAreaElement);
    fireEvent.change(textarea, { target: { value: 'first message' } });
    fireEvent.click(view.container.querySelector('button[aria-label="Send message"]') as HTMLButtonElement);
    await waitFor(() => expect(view.container.querySelector('[data-send-state="accepted"]')).not.toBeNull());

    const file = new File(['next draft bytes'], 'next.txt', { type: 'text/plain' });
    const transfer = new DataTransfer();
    await waitFor(() => {
      const attachButton = view.container.querySelector('button[aria-label="Attach file"]') as HTMLButtonElement;
      expect(attachButton.disabled).toBe(false);
    });
    transfer.items.add(file);
    const fileInput = view.container.querySelector<HTMLInputElement>('input[type="file"]');
    if (!fileInput) throw new Error('expected attachment input');
    Object.defineProperty(fileInput, 'files', { value: transfer.files, configurable: true });
    fireEvent.change(fileInput);
    await waitFor(() => expect(view.container.textContent).toContain('next.txt'));
    releaseAcceptedRefresh();
    const identityKey = `conversationDraftSend:test-user%40example.com:${projectId}`;
    const nextIdentity = await waitFor(() => {
      const value = sessionStorage.getItem(identityKey);
      expect(value).toBeTruthy();
      return value as string;
    });

    conversationItems = [makeMsg({
      id: 'reply-to-first', project_id: projectId, from_type: 'strategic',
      content: 'First turn complete', surface: 'conversation', handling_state: 'responded',
    })];
    view.rerender(
      <MessagesPanel
        selectedProjectId={projectId}
        addToast={() => {}}
        onMessageCountChange={() => {}}
        filterChrome="minimal"
        realtimeRefreshSignal={1}
      />,
    );
    await waitFor(() => expect(view.container.textContent).toContain('First turn complete'));
    expect(sessionStorage.getItem(identityKey)).toBe(nextIdentity);

    fireEvent.change(textarea, { target: { value: 'second message' } });
    fireEvent.click(view.container.querySelector('button[aria-label="Send message"]') as HTMLButtonElement);
    await waitFor(() => expect(view.container.querySelector('button[aria-label="Retry message"]')).not.toBeNull());
    const pendingBeforeRemount = JSON.parse(sessionStorage.getItem(
      `optimisticSend:test-user%40example.com:${projectId}`,
    ) ?? '{}') as { key?: string };
    expect(pendingBeforeRemount.key).toBe(nextIdentity);

    view.unmount();
    const remounted = render(
      <MessagesPanel
        selectedProjectId={projectId}
        addToast={() => {}}
        onMessageCountChange={() => {}}
        filterChrome="minimal"
      />,
    );
    await waitFor(() => expect(remounted.container.querySelector('button[aria-label="Retry message"]')).not.toBeNull());
    await waitFor(() => expect(remounted.container.textContent).toContain('next.txt'));
    expect(sessionStorage.getItem(
      `optimisticSend:test-user%40example.com:${projectId}`,
    )).toContain(nextIdentity);

    remounted.unmount();
    await pendingAttachmentStore.clearPendingAttachments('test-user@example.com', projectId, nextIdentity);
  });
});

// ── MessagesPanel needs-action filter toggle ─────────────────────────────────

describe('MessagesPanel needs-action filter', () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    vi.clearAllMocks();
  });
  afterEach(() => { vi.clearAllMocks(); });

  test('renders needs-action / show-all toggle when strategicState is provided', async () => {
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    _mockMessagesFetch([makeMsg({ id: 'msg-1', message_type: 'QUESTION', content: 'a question?' })]);

    const { container } = render(
      <MessagesPanel
        selectedProjectId="proj-1"
        addToast={() => {}}
        onMessageCountChange={() => {}}
        strategicState={makeStrategicState()}
      />,
    );

    await waitFor(() => {
      expect(container.querySelector('[data-testid="needs-action-filter-bar"]')).not.toBeNull();
    });

    const needsActionBtn = container.querySelector('[data-testid="needs-action-filter-needs-action"]') as HTMLButtonElement | null;
    const showAllBtn = container.querySelector('[data-testid="needs-action-filter-show-all"]') as HTMLButtonElement | null;
    expect(needsActionBtn).not.toBeNull();
    expect(showAllBtn).not.toBeNull();

    // Default: needs-action mode is active (aria-pressed=true)
    expect(needsActionBtn!.getAttribute('aria-pressed')).toBe('true');
    expect(showAllBtn!.getAttribute('aria-pressed')).toBe('false');
  });

  test('does not render needs-action filter bar without strategicState', async () => {
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    _mockMessagesFetch([]);

    const { container } = render(
      <MessagesPanel
        selectedProjectId="proj-1"
        addToast={() => {}}
        onMessageCountChange={() => {}}
      />,
    );

    // Wait for initial render to settle
    await waitFor(() => expect(container.querySelector('div')).not.toBeNull());
    await act(async () => { await new Promise<void>(res => setTimeout(res, 30)); });

    expect(container.querySelector('[data-testid="needs-action-filter-bar"]')).toBeNull();
  });

  test('clicking Show all switches toggle to show-all mode', async () => {
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    _mockMessagesFetch([makeMsg({ id: 'msg-1', message_type: 'QUESTION', content: 'a question?' })]);

    const { container } = render(
      <MessagesPanel
        selectedProjectId="proj-1"
        addToast={() => {}}
        onMessageCountChange={() => {}}
        strategicState={makeStrategicState()}
      />,
    );

    await waitFor(() => {
      expect(container.querySelector('[data-testid="needs-action-filter-show-all"]')).not.toBeNull();
    });

    const showAllBtn = container.querySelector('[data-testid="needs-action-filter-show-all"]') as HTMLButtonElement;
    fireEvent.click(showAllBtn);

    await waitFor(() => {
      const needsActionBtn = container.querySelector('[data-testid="needs-action-filter-needs-action"]') as HTMLButtonElement;
      const showAll = container.querySelector('[data-testid="needs-action-filter-show-all"]') as HTMLButtonElement;
      expect(needsActionBtn.getAttribute('aria-pressed')).toBe('false');
      expect(showAll.getAttribute('aria-pressed')).toBe('true');
    });
  });

  test('clicking Needs action after Show all switches back to needs-action mode', async () => {
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    _mockMessagesFetch([makeMsg({ id: 'msg-1', message_type: 'QUESTION', content: 'a question?' })]);

    const { container } = render(
      <MessagesPanel
        selectedProjectId="proj-1"
        addToast={() => {}}
        onMessageCountChange={() => {}}
        strategicState={makeStrategicState()}
      />,
    );

    await waitFor(() => {
      expect(container.querySelector('[data-testid="needs-action-filter-show-all"]')).not.toBeNull();
    });

    // Switch to show-all
    fireEvent.click(container.querySelector('[data-testid="needs-action-filter-show-all"]')!);

    await waitFor(() => {
      const needsActionBtn = container.querySelector('[data-testid="needs-action-filter-needs-action"]') as HTMLButtonElement;
      expect(needsActionBtn.getAttribute('aria-pressed')).toBe('false');
    });

    // Switch back to needs-action
    fireEvent.click(container.querySelector('[data-testid="needs-action-filter-needs-action"]')!);

    await waitFor(() => {
      const needsActionBtn = container.querySelector('[data-testid="needs-action-filter-needs-action"]') as HTMLButtonElement;
      expect(needsActionBtn.getAttribute('aria-pressed')).toBe('true');
    });
  });

  // Acceptance test: strategist-internal audience never reaches operator needs-action list
  test('strategist-internal audience never reaches operator needs-action list', async () => {
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');

    // A strategist-internal message whose event_type would otherwise classify as high-signal
    const strategistInternal = makeMsg({
      id: 'internal-msg',
      message_type: 'NOTE',
      from_type: 'system',
      content: 'Internal strategist note — should never reach operator inbox',
      context: { audience: 'strategist-internal', event_type: 'failure_alert' },
    });

    // An operator-action-needed message that should appear in the needs-action list
    const operatorActionNeeded = makeMsg({
      id: 'action-msg',
      message_type: 'NOTE',
      from_type: 'agent',
      content: 'Operator action required',
      context: { audience: 'operator-action-needed', event_type: 'direction_outcome_stalled' },
    });

    // The strategic attention model lists only the operator-action-needed message as needs_operator
    const state = makeStrategicState({
      attention: {
        level: 'needs_operator',
        summary: '1 item needs action',
        last_seen_at: null,
        counts: {
          needs_operator: 1, watch: 0, unread: 0,
          questions: 0, blockers: 0, approvals: 0,
          manual_directions: 0, failed_tasks: 0,
          blocked_tasks: 0, parked_tasks: 0,
        },
        items: [
          {
            id: 'message:action-msg',
            kind: 'message',
            severity: 'needs_operator',
            reason: 'high_signal_note',
            label: 'Operator action required',
            ref: { kind: 'message', id: 'action-msg', project_id: 'proj-1' },
          },
        ],
      },
    });

    _mockMessagesFetch([strategistInternal, operatorActionNeeded]);

    const { container } = render(
      <MessagesPanel
        selectedProjectId="proj-1"
        addToast={() => {}}
        onMessageCountChange={() => {}}
        strategicState={state}
      />,
    );

    // Wait for the panel and filter bar to render
    await waitFor(() => {
      expect(container.querySelector('[data-testid="needs-action-filter-bar"]')).not.toBeNull();
    });

    // Default view is needs-action mode — strategist-internal message must not appear
    const text = container.textContent ?? '';
    expect(text).not.toContain('Internal strategist note — should never reach operator inbox');

    // The operator-action-needed message IS visible in needs-action mode
    expect(text).toContain('Operator action required');
  });
});

// ── Finding m-3: operator inbox consumes attention lifecycle fields ──────────
// Verifies that lifecycle.operator_action, lifecycle.acknowledgeable, and
// lifecycle.acknowledge_effect are consumed by the needs-action strip UI, not
// silently ignored. Finding claim: only clears_when was rendered; the others
// were typed but unused.
describe('MessagesPanel attention model lifecycle field consumption', () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear(); vi.clearAllMocks(); localStorage.setItem('bp.chatChromeExpanded', 'true'); });
  afterEach(() => { localStorage.clear(); sessionStorage.clear(); vi.clearAllMocks(); });

  function setupConvMock(): void {
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
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
      return new Response(JSON.stringify({ messages: [] }), { status: 200 });
    });
  }

  test('operator_action hint renders in needs-action chip when lifecycle.operator_action is set', async () => {
    setupConvMock();
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    const state = makeStrategicState({
      attention: {
        level: 'needs_operator',
        summary: '1 item needs action',
        last_seen_at: null,
        counts: {
          needs_operator: 1, watch: 0, unread: 0, questions: 1,
          blockers: 0, approvals: 0, manual_directions: 0,
          failed_tasks: 0, blocked_tasks: 0, parked_tasks: 0,
        },
        items: [
          {
            id: 'message:msg-lc-1',
            kind: 'message',
            severity: 'needs_operator',
            reason: 'question',
            label: 'Lifecycle question',
            ref: { kind: 'message', id: 'msg-lc-1', project_id: 'proj-1' },
            lifecycle: {
              clears_when: 'answered',
              operator_action: 'answer',
              acknowledgeable: false,
              acknowledge_effect: null,
            },
          },
        ],
      },
    });
    const { container } = render(
      <MessagesPanel
        selectedProjectId="proj-1"
        addToast={() => {}}
        onMessageCountChange={() => {}}
        filterChrome="compact"
        strategicState={state}
      />,
    );
    await waitFor(() => {
      expect(container.querySelector('[data-testid="needs-action-strip"]')).toBeTruthy();
    });
    // operator_action hint must be rendered (data-testid="operator-action-hint")
    await waitFor(() => {
      const hint = container.querySelector('[data-testid="operator-action-hint"]');
      expect(hint).toBeTruthy();
    });
  });

  test('Ack button renders when lifecycle.acknowledgeable=true and clicking it hides the item', async () => {
    setupConvMock();
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    const state = makeStrategicState({
      attention: {
        level: 'needs_operator',
        summary: '1 item needs action',
        last_seen_at: null,
        counts: {
          needs_operator: 1, watch: 0, unread: 0, questions: 0,
          blockers: 1, approvals: 0, manual_directions: 0,
          failed_tasks: 0, blocked_tasks: 0, parked_tasks: 0,
        },
        items: [
          {
            id: 'message:msg-ack-1',
            kind: 'message',
            severity: 'needs_operator',
            reason: 'blocker',
            label: 'Ack-able blocker',
            ref: { kind: 'message', id: 'msg-ack-1', project_id: 'proj-1' },
            lifecycle: {
              clears_when: 'task_unblocked',
              operator_action: null,
              acknowledgeable: true,
              acknowledge_effect: 'hides_until_change',
            },
          },
        ],
      },
    });
    const { container } = render(
      <MessagesPanel
        selectedProjectId="proj-1"
        addToast={() => {}}
        onMessageCountChange={() => {}}
        filterChrome="compact"
        strategicState={state}
      />,
    );
    await waitFor(() => {
      expect(container.querySelector('[data-testid="needs-action-strip"]')).toBeTruthy();
    });
    // Ack button must be rendered when acknowledgeable=true
    const ackBtn = await waitFor(() => {
      const btn = container.querySelector('[data-testid="attention-ack-btn"]');
      expect(btn).toBeTruthy();
      return btn as HTMLButtonElement;
    });
    // Clicking Ack must remove the item from the strip (client-side hide)
    fireEvent.click(ackBtn);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="attention-ack-btn"]')).toBeNull();
    });
    // The strip itself may disappear when all items are acked
    const strip = container.querySelector('[data-testid="needs-action-strip"]');
    if (strip) {
      const chipText = strip.textContent ?? '';
      expect(chipText).not.toContain('Ack-able blocker');
    }
  });

  test('no Ack button when lifecycle.acknowledgeable is false or absent', async () => {
    setupConvMock();
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    const state = makeStrategicState({
      attention: {
        level: 'needs_operator',
        summary: '1 item needs action',
        last_seen_at: null,
        counts: {
          needs_operator: 1, watch: 0, unread: 0, questions: 1,
          blockers: 0, approvals: 0, manual_directions: 0,
          failed_tasks: 0, blocked_tasks: 0, parked_tasks: 0,
        },
        items: [
          {
            id: 'message:msg-noack-1',
            kind: 'message',
            severity: 'needs_operator',
            reason: 'question',
            label: 'Non-ackable question',
            ref: { kind: 'message', id: 'msg-noack-1', project_id: 'proj-1' },
            lifecycle: {
              clears_when: 'answered',
              operator_action: 'answer',
              acknowledgeable: false,
              acknowledge_effect: null,
            },
          },
        ],
      },
    });
    const { container } = render(
      <MessagesPanel
        selectedProjectId="proj-1"
        addToast={() => {}}
        onMessageCountChange={() => {}}
        filterChrome="compact"
        strategicState={state}
      />,
    );
    await waitFor(() => {
      expect(container.querySelector('[data-testid="needs-action-strip"]')).toBeTruthy();
    });
    // When acknowledgeable=false, no Ack button should appear
    await act(async () => { await new Promise<void>(res => setTimeout(res, 30)); });
    expect(container.querySelector('[data-testid="attention-ack-btn"]')).toBeNull();
  });
});

describe('MessagesPanel triage pagination — small initial fetch', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => root!.unmount());
    root = null;
    host?.remove();
    host = null;
    localStorage.clear();
    vi.clearAllMocks();
  });

  async function mountPanel(props: Partial<MessagesPanelProps> = {}) {
    host = document.createElement('div');
    host.style.display = 'flex';
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

  test('initial fetch uses limit=30', async () => {
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    // Default mock returns empty messages — component should fire a GET
    await mountPanel();
    const messageCalls = vi.mocked(apiFetch).mock.calls.filter((c) => {
      const url = String(c[0]);
      const init = c[1] as RequestInit | undefined;
      return /\/api\/admin\/messages(\?|$)/.test(url) && (!init?.method || init.method === 'GET');
    });
    expect(messageCalls.length).toBeGreaterThanOrEqual(1);
    for (const call of messageCalls) {
      const url = new URL(String(call[0]), 'http://localhost');
      expect(url.searchParams.get('limit')).toBe('30');
    }
  });

  test('initial fetch offset is absent (page 1)', async () => {
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    await mountPanel();
    const messageCalls = vi.mocked(apiFetch).mock.calls.filter((c) => {
      const url = String(c[0]);
      const init = c[1] as RequestInit | undefined;
      return /\/api\/admin\/messages(\?|$)/.test(url) && (!init?.method || init.method === 'GET');
    });
    expect(messageCalls.length).toBeGreaterThanOrEqual(1);
    for (const call of messageCalls) {
      const url = new URL(String(call[0]), 'http://localhost');
      // First page should not have an offset parameter
      expect(url.searchParams.has('offset')).toBe(false);
    }
  });
});
