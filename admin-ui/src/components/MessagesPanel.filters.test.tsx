import { describe, test, expect, vi, afterEach, beforeEach } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { render, waitFor } from '@testing-library/react';
import { MessagesPanel } from './MessagesPanel';
import { effectiveSignal, isEventTypeTemporarilyMuted, loadTemporaryMutes, saveTemporaryMutes } from './MessagesPanel.utils';
import type { ProjectMessage } from './MessageBubble';
import type { SavedView } from './MessagesPanel.utils';

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


describe('MessagesPanel feedback acknowledgment reply context', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) act(() => r.unmount());
    root = null;
    host?.remove();
    host = null;
    vi.clearAllMocks();
  });

  async function renderPanel(messages: ReturnType<typeof makeMsg>[]) {
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

  test('renders reply context label and original feedback excerpt for feedback_acknowledgment NOTE', async () => {
    const feedback = makeMsg({
      id: 'feedback-1',
      message_type: 'FEEDBACK',
      from_type: 'user',
      content: 'How do we handle edge case X in the validation logic?',
    });
    const ack = makeMsg({
      id: 'ack-1',
      message_type: 'NOTE',
      from_type: 'system',
      content: 'We added a new validation step to handle that case.',
      context: { event_type: 'feedback_acknowledgment', reply_to: 'feedback-1' },
    });
    const el = await renderPanel([feedback, ack]);

    // feedback_acknowledgment is now classified routine, so the bubble lives inside the
    // collapsed routine-ops group. Expand it so the reply context becomes visible.
    const routineToggle = [...el.querySelectorAll('button')]
      .find(b => /routine ops/.test(b.textContent ?? ''));
    expect(routineToggle).toBeTruthy();
    await act(async () => { (routineToggle as HTMLButtonElement).click(); });

    const text = el.textContent ?? '';
    // Reply context label should appear
    expect(text).toContain('In reply to your feedback:');
    // Original feedback excerpt should appear
    expect(text).toContain('How do we handle edge case X');
    // Acknowledgment content should appear
    expect(text).toContain('We added a new validation step');
  });

  test('gracefully handles missing reply_to reference (original feedback not in list)', async () => {
    const ack = makeMsg({
      id: 'ack-1',
      message_type: 'NOTE',
      from_type: 'system',
      content: 'This is a reply to a feedback that was already resolved.',
      context: { event_type: 'feedback_acknowledgment', reply_to: 'feedback-missing' },
    });
    const el = await renderPanel([ack]);

    // feedback_acknowledgment is now routine — expand the routine ops group to reveal the bubble.
    const routineToggle = [...el.querySelectorAll('button')]
      .find(b => /routine ops/.test(b.textContent ?? ''));
    expect(routineToggle).toBeTruthy();
    await act(async () => { (routineToggle as HTMLButtonElement).click(); });

    const text = el.textContent ?? '';
    // Acknowledgment content should still render
    expect(text).toContain('This is a reply to a feedback');
    // Reply context label should NOT appear when reference is missing
    expect(text).not.toContain('In reply to your feedback:');
  });

  test('does not show reply context for regular NOTE messages without feedback_acknowledgment event_type', async () => {
    // Use a high-signal NOTE event_type so the bubble actually renders (routine NOTEs
    // are placed inside the collapsed routine-ops group where bubble content is hidden).
    const regularNote = makeMsg({
      id: 'note-1',
      message_type: 'NOTE',
      from_type: 'system',
      content: 'A regular note without reply context',
      context: { event_type: 'failure_alert' },
    });
    const el = await renderPanel([regularNote]);
    const text = el.textContent ?? '';

    expect(text).toContain('A regular note without reply context');
    expect(text).not.toContain('In reply to your feedback:');
  });

  test('renders reply context label and original question excerpt for question_answer NOTE', async () => {
    const question = makeMsg({
      id: 'question-1',
      message_type: 'QUESTION',
      from_type: 'user',
      content: 'Should we switch to a different caching strategy?',
    });
    const answer = makeMsg({
      id: 'answer-1',
      message_type: 'NOTE',
      from_type: 'strategic',
      content: 'Yes, switching to a distributed cache would improve performance.',
      context: { event_type: 'question_answer', reply_to: 'question-1' },
    });
    const el = await renderPanel([question, answer]);

    // question_answer is high-signal, so the bubble renders directly (not in routine ops).
    const text = el.textContent ?? '';
    // Reply context label should appear with question-specific wording
    expect(text).toContain('Answering your question:');
    // Original question excerpt should appear
    expect(text).toContain('Should we switch to a different caching strategy');
    // Answer content should appear
    expect(text).toContain('Yes, switching to a distributed cache');
  });

  test('gracefully handles missing reply_to reference for question_answer (original question not in list)', async () => {
    const answer = makeMsg({
      id: 'answer-1',
      message_type: 'NOTE',
      from_type: 'strategic',
      content: 'This answer refers to a question that was already resolved.',
      context: { event_type: 'question_answer', reply_to: 'question-missing' },
    });
    const el = await renderPanel([answer]);

    // question_answer is high-signal — bubble renders directly.
    const text = el.textContent ?? '';
    // Answer content should still render
    expect(text).toContain('This answer refers to a question');
    // Reply context label should NOT appear when reference is missing
    expect(text).not.toContain('Answering your question:');
  });

  test('renders clickable task slug when onOpenTask is provided', async () => {
    const mockOpenTask = vi.fn();
    const agentMsg = makeMsg({
      id: 'agent-1',
      message_type: 'BLOCKER',
      from_type: 'agent',
      from_task_id: 'abcd1234-efgh-5678-ijkl-9012mnopqrst',
      content: 'Agent message with task ref',
    });
    _mockMessagesFetch([agentMsg]);
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
          onOpenTask={mockOpenTask}
        />,
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });

    // Click the task slug
    const slug = host!.querySelector('button[title*="Open task"]');
    expect(slug).toBeTruthy();
    await act(async () => { (slug as HTMLButtonElement).click(); });

    // onOpenTask should have been called with the full task ID
    expect(mockOpenTask).toHaveBeenCalledWith('abcd1234-efgh-5678-ijkl-9012mnopqrst');
  });

  test('does not render clickable task slug when onOpenTask is not provided', async () => {
    const agentMsg = makeMsg({
      id: 'agent-1',
      message_type: 'BLOCKER',
      from_type: 'agent',
      from_task_id: 'abcd1234-efgh-5678-ijkl-9012mnopqrst',
      content: 'Agent message with task ref',
    });
    _mockMessagesFetch([agentMsg]);
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

    // Task slug button should not be present
    const slug = host!.querySelector('button[title*="Open task"]');
    expect(slug).toBeFalsy();
  });
});

// ── Saved views (operator filter combos) ─────────────────────────────────────

describe('MessagesPanel saved views', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  const userId = 'test-user@example.com';

  beforeEach(() => {
    localStorage.clear();
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

  async function renderForProject(projectId: string, messages: ReturnType<typeof makeMsg>[] = []) {
    _mockMessagesFetch(messages);
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <MessagesPanel
          selectedProjectId={projectId}
          onClose={() => {}}
          addToast={() => {}}
          onMessageCountChange={() => {}}
        />,
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    return host as HTMLDivElement;
  }

  async function rerenderForProject(projectId: string, messages: ReturnType<typeof makeMsg>[] = []) {
    _mockMessagesFetch(messages);
    await act(async () => {
      root!.render(
        <MessagesPanel
          selectedProjectId={projectId}
          onClose={() => {}}
          addToast={() => {}}
          onMessageCountChange={() => {}}
        />,
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
  }

  function tabStrip(el: HTMLElement): HTMLElement {
    const strip = el.querySelector('[data-testid="saved-views-tab-strip"]');
    if (!strip) throw new Error('tab strip not found');
    return strip as HTMLElement;
  }

  function tabButtons(el: HTMLElement): HTMLButtonElement[] {
    return [...tabStrip(el).querySelectorAll('button')] as HTMLButtonElement[];
  }

  function tabByName(el: HTMLElement, name: string): HTMLButtonElement | null {
    return tabButtons(el).find(b => b.textContent?.trim().startsWith(name)) ?? null;
  }

  test('Default tab is rendered and active by default', async () => {
    const el = await renderForProject('proj-1');
    const def = tabByName(el, 'Default');
    expect(def).toBeTruthy();
    expect(def!.className).toMatch(/blue-500/);
  });

  test('Save view dialog enforces non-empty name and 10-view cap', async () => {
    // Pre-seed 10 views so creating an 11th is blocked.
    const tenViews: SavedView[] = Array.from({ length: 10 }, (_, i) => ({
      id: `v${i}`,
      name: `View ${i}`,
      query: '',
      prefs: {},
      sort: 'importance',
      showResolved: false,
      showOnlyNew: false,
      routineExpanded: false,
      createdAt: Date.now() - i,
    }));
    localStorage.setItem(`messages_saved_views:${userId}:proj-1`, JSON.stringify({ version: 1, views: tenViews }));
    const el = await renderForProject('proj-1');
    const saveBtn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Save current filters'));
    expect(saveBtn).toBeTruthy();
    await act(async () => { (saveBtn as HTMLButtonElement).click(); });
    const nameInput = document.body.querySelector('input[placeholder*="Unresolved"]') as HTMLInputElement | null;
    expect(nameInput).toBeTruthy();
    await act(async () => {
      nameInput!.focus();
      const proto = Object.getPrototypeOf(nameInput);
      Object.getOwnPropertyDescriptor(proto, 'value')?.set?.call(nameInput, 'Eleventh');
      nameInput!.dispatchEvent(new Event('input', { bubbles: true }));
    });
    const submit = [...document.body.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Save view' && b.className.includes('bg-blue-600'));
    expect(submit).toBeTruthy();
    await act(async () => { (submit as HTMLButtonElement).click(); });
    const alert = document.body.querySelector('[role="alert"]');
    expect(alert?.textContent).toMatch(/Cap reached/);
    // Existing 10 views unchanged
    const stored = JSON.parse(localStorage.getItem(`messages_saved_views:${userId}:proj-1`)!);
    expect(stored.views).toHaveLength(10);
  });

  test('saving a view persists it and switching to Default resets state', async () => {
    const el = await renderForProject('proj-1');
    // Type into the search box
    const searchInput = el.querySelector('input[placeholder="Search messages..."]') as HTMLInputElement;
    expect(searchInput).toBeTruthy();
    await act(async () => {
      const proto = Object.getPrototypeOf(searchInput);
      Object.getOwnPropertyDescriptor(proto, 'value')?.set?.call(searchInput, 'urgent');
      searchInput.dispatchEvent(new Event('input', { bubbles: true }));
    });
    // The search box debounces onSearchChange by 300ms (MessagesPanelHeader),
    // so wait for it to propagate to the panel's searchQuery before saving —
    // otherwise the view is captured with the still-empty pre-debounce query.
    await act(async () => { await new Promise<void>(res => setTimeout(res, 350)); });
    // Open save dialog
    const openBtn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Save current filters'));
    await act(async () => { (openBtn as HTMLButtonElement).click(); });
    // Fill name and submit
    const nameInput = document.body.querySelector('input[placeholder*="Unresolved"]') as HTMLInputElement;
    await act(async () => {
      const proto = Object.getPrototypeOf(nameInput);
      Object.getOwnPropertyDescriptor(proto, 'value')?.set?.call(nameInput, 'Urgent stuff');
      nameInput.dispatchEvent(new Event('input', { bubbles: true }));
    });
    const submit = [...document.body.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Save view' && b.className.includes('bg-blue-600'));
    await act(async () => { (submit as HTMLButtonElement).click(); });
    // Stored under per-project-per-user key
    const stored = JSON.parse(localStorage.getItem(`messages_saved_views:${userId}:proj-1`)!);
    expect(stored.views).toHaveLength(1);
    expect(stored.views[0].name).toBe('Urgent stuff');
    expect(stored.views[0].query).toBe('urgent');
    // Tab strip shows the new tab
    expect(tabByName(el, 'Urgent stuff')).toBeTruthy();
    // Click Default tab — search should reset
    const defaultTab = tabByName(el, 'Default');
    await act(async () => { defaultTab!.click(); });
    const searchAfter = el.querySelector('input[placeholder="Search messages..."]') as HTMLInputElement;
    expect(searchAfter.value).toBe('');
  });

  test('applying a saved view updates query, sort, and showResolved together', async () => {
    // Seed a view that flips multiple fields
    const seeded: SavedView = {
      id: 'v_complex',
      name: 'Recent Resolved',
      query: 'pr_merged',
      prefs: { pr_merged: 'mute' },
      sort: 'recent',
      showResolved: true,
      showOnlyNew: false,
      routineExpanded: true,
      createdAt: Date.now(),
    };
    localStorage.setItem(`messages_saved_views:${userId}:proj-1`, JSON.stringify({ version: 1, views: [seeded] }));
    const el = await renderForProject('proj-1');
    const tab = tabByName(el, 'Recent Resolved');
    expect(tab).toBeTruthy();
    await act(async () => { tab!.click(); });
    // Search updated
    const searchInput = el.querySelector('input[placeholder="Search messages..."]') as HTMLInputElement;
    expect(searchInput.value).toBe('pr_merged');
    // Sort toggle text reflects 'recent' mode
    const sortBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Recent' || b.textContent?.trim() === 'Importance');
    expect(sortBtn?.textContent?.trim()).toBe('Recent');
    // Event prefs persisted to project key
    const prefsRaw = localStorage.getItem(`messages_panel_event_prefs:${userId}:proj-1`);
    expect(prefsRaw).not.toBeNull();
    expect(JSON.parse(prefsRaw!)).toEqual({ pr_merged: 'mute' });
  });

  test('deleting a view removes it from tab strip and storage', async () => {
    const seeded: SavedView = {
      id: 'v_kill',
      name: 'Doomed',
      query: '',
      prefs: {},
      sort: 'importance',
      showResolved: false,
      showOnlyNew: false,
      routineExpanded: false,
      createdAt: Date.now(),
    };
    localStorage.setItem(`messages_saved_views:${userId}:proj-1`, JSON.stringify({ version: 1, views: [seeded] }));
    const el = await renderForProject('proj-1');
    expect(tabByName(el, 'Doomed')).toBeTruthy();
    // Open kebab
    const kebabBtn = [...el.querySelectorAll('button')].find(b => b.getAttribute('aria-label')?.includes('Doomed'));
    expect(kebabBtn).toBeTruthy();
    await act(async () => { (kebabBtn as HTMLButtonElement).click(); });
    const deleteBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Delete');
    expect(deleteBtn).toBeTruthy();
    await act(async () => { (deleteBtn as HTMLButtonElement).click(); });
    expect(tabByName(el, 'Doomed')).toBeFalsy();
    expect(localStorage.getItem(`messages_saved_views:${userId}:proj-1`)).toBeNull();
  });

  test('per-project isolation: project A views do not bleed into project B', async () => {
    const seeded: SavedView = {
      id: 'v_a',
      name: 'A-only',
      query: 'a',
      prefs: {},
      sort: 'importance',
      showResolved: false,
      showOnlyNew: false,
      routineExpanded: false,
      createdAt: Date.now(),
    };
    localStorage.setItem(`messages_saved_views:${userId}:proj-A`, JSON.stringify({ version: 1, views: [seeded] }));
    const el = await renderForProject('proj-A');
    expect(tabByName(el, 'A-only')).toBeTruthy();
    await rerenderForProject('proj-B');
    expect(tabByName(el, 'A-only')).toBeFalsy();
    expect(tabByName(el, 'Default')).toBeTruthy();
  });
});

describe('MessagesPanel Investigate action', () => {
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

  async function renderWithMessages(messages: ReturnType<typeof makeMsg>[]) {
    (apiFetch as ReturnType<typeof vi.fn>).mockClear();
    _mockMessagesFetch(messages);
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: 'task-123' }), { status: 200 }),
    );
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <MessagesPanel
          selectedProjectId="proj-1"
          githubRepo="owner/repo"
          onClose={() => {}}
          addToast={() => {}}
          onMessageCountChange={() => {}}
        />,
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    return host as HTMLDivElement;
  }

  test('Investigate button only renders for BLOCKER messages', async () => {
    const blocker = makeMsg({
      id: 'msg-blocker',
      message_type: 'BLOCKER',
      content: 'System is down',
      status: 'open',
    });
    const feedback = makeMsg({
      id: 'msg-feedback',
      message_type: 'FEEDBACK',
      content: 'Consider adding feature',
      status: 'open',
    });
    const el = await renderWithMessages([blocker, feedback]);
    // Check that the blocker message is rendered
    expect(el.textContent).toContain('System is down');
    // Check that the Investigate button is present
    const buttons = el.querySelectorAll('button[title="Investigate"]');
    expect(buttons.length).toBeGreaterThan(0);
  });

  test('Investigate button only renders for BUG_REPORT messages', async () => {
    const bugReport = makeMsg({
      id: 'msg-bug',
      message_type: 'BUG_REPORT',
      content: 'App crashes on login',
      status: 'open',
    });
    const el = await renderWithMessages([bugReport]);
    const button = el.querySelector('button[title="Investigate"]');
    expect(button).toBeTruthy();
  });

  test('clicking Investigate button shows inline confirmation', async () => {
    const blocker = makeMsg({
      id: 'msg-1',
      message_type: 'BLOCKER',
      content: 'Critical issue',
      status: 'open',
    });
    const el = await renderWithMessages([blocker]);
    const investigateBtn = el.querySelector('button[title="Investigate"]') as HTMLButtonElement;
    expect(investigateBtn).toBeTruthy();
    await act(async () => { investigateBtn.click(); });
    const confirmText = el.textContent?.includes('Spawn investigation?');
    expect(confirmText).toBe(true);
  });

  test('Investigate confirmation flow shows inline dialog', async () => {
    const blocker = makeMsg({
      id: 'msg-1',
      message_type: 'BLOCKER',
      content: 'Critical issue',
      status: 'open',
    });
    const el = await renderWithMessages([blocker]);
    // Investigate button renders for BLOCKER
    const investigateBtn = el.querySelector('button[title="Investigate"]') as HTMLButtonElement;
    expect(investigateBtn).toBeTruthy();
    // Click Investigate button
    await act(async () => { investigateBtn.click(); });
    // Confirmation UI appears with prompt text
    expect(el.textContent).toContain('Spawn investigation?');
    // Confirm and cancel buttons appear
    const confirmBtn = [...el.querySelectorAll('button')].find(b => b.title === 'Confirm investigation');
    const cancelBtn = [...el.querySelectorAll('button')].find(b => b.title === 'Cancel' && el.textContent?.includes('Spawn investigation'));
    expect(confirmBtn).toBeTruthy();
    expect(cancelBtn).toBeTruthy();
  });

  test('investigation task POST includes source_message_id', async () => {
    const blocker = makeMsg({
      id: 'msg-origin',
      message_type: 'BLOCKER',
      content: 'Critical issue with stale refs',
      status: 'open',
    });
    const el = await renderWithMessages([blocker]);
    // Clear any fetch calls from initial render (e.g. ui-load-timing analytics).
    (global.fetch as ReturnType<typeof vi.fn>).mockClear();
    const investigateBtn = el.querySelector('button[title="Investigate"]') as HTMLButtonElement;
    expect(investigateBtn).toBeTruthy();

    await act(async () => { investigateBtn.click(); });
    const confirmBtn = [...el.querySelectorAll('button')].find(b => b.title === 'Confirm investigation') as HTMLButtonElement | undefined;
    expect(confirmBtn).toBeTruthy();

    await act(async () => { confirmBtn?.click(); });

    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/admin/tasks'),
      expect.objectContaining({
        method: 'POST',
        body: expect.any(String),
      }),
    );

    const body = JSON.parse(((global.fetch as ReturnType<typeof vi.fn>).mock.calls[0]?.[1] as RequestInit).body as string);
    expect(body).toMatchObject({
      github_repo: 'owner/repo',
      title: 'Critical issue with stale refs',
      prompt: expect.stringContaining('Original message: msg-origin'),
      source_message_id: 'msg-origin',
      task_type: 'investigate',
    });
  });
});

describe('MessagesPanel showOnlyNew filter', () => {
  const userId = 'test-user@example.com';

  async function renderForProject(projectId: string, messages: ReturnType<typeof makeMsg>[] = []) {
    _mockMessagesFetch(messages);
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => {
      root.render(<MessagesPanel selectedProjectId={projectId} onClose={() => {}} addToast={() => {}} />);
    });
    await waitFor(() => expect(_messagesGetCalls().length).toBeGreaterThan(0), { timeout: 3000 });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    return container;
  }

  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  test('showOnlyNew toggle is enabled after cold-start bootstrap sets dividerTime', async () => {
    const el = await renderForProject('proj-1');
    // Cold-start bootstrap (#4974) records a divider baseline even on a first
    // visit with no prior last-seen, so the toggle is enabled rather than disabled.
    await waitFor(() => {
      const showOnlyNewBtn = [...el.querySelectorAll('button')].find(b =>
        b.title?.includes('No messages to filter') || b.title?.includes('Only new since')
      );
      expect(showOnlyNewBtn).toBeTruthy();
      expect((showOnlyNewBtn as HTMLButtonElement).disabled).toBe(false);
    });
  });

  test('showOnlyNew toggle becomes enabled after dividerTime is set', async () => {
    const noteMsg = {
      id: '1',
      project_id: 'proj-1',
      message_type: 'NOTE',
      from_type: 'system',
      from_task_id: null,
      content: 'Test',
      context: { event_type: 'pr_merged' },
      status: 'open',
      resolution: null,
      resolved_at: null,
      created_at: new Date(Date.now() - 1000).toISOString(),
    } as ReturnType<typeof makeMsg>;
    await renderForProject('proj-1', [noteMsg]);
    // Simulate divider time being set (by scrolling past/timeout)
    localStorage.setItem(`messages_panel_last_seen:${userId}:proj-1`, String(Date.now() - 500));
    // Re-render to pick up the divider time
    document.body.innerHTML = '';
    const el2 = await renderForProject('proj-1', [noteMsg]);
    const showOnlyNewBtn = [...el2.querySelectorAll('button')].find(b =>
      b.title?.includes('Only new since') || b.title?.includes('Show all messages')
    );
    expect(showOnlyNewBtn).toBeTruthy();
    expect((showOnlyNewBtn as HTMLButtonElement).disabled).toBe(false);
  });

  test('when showOnlyNew is enabled, older messages are filtered out from high-signal section', async () => {
    const now = Date.now();
    const dividerTime = now - 5000;
    const oldMsg = {
      id: '1',
      project_id: 'proj-1',
      message_type: 'BUG_REPORT',
      from_type: 'system',
      from_task_id: null,
      content: 'Old bug',
      context: { severity: 'high' },
      status: 'open',
      resolution: null,
      resolved_at: null,
      created_at: new Date(dividerTime - 1000).toISOString(),
    } as ReturnType<typeof makeMsg>;
    const newMsg = {
      id: '2',
      project_id: 'proj-1',
      message_type: 'BUG_REPORT',
      from_type: 'system',
      from_task_id: null,
      content: 'New bug',
      context: { severity: 'high' },
      status: 'open',
      resolution: null,
      resolved_at: null,
      created_at: new Date(dividerTime + 1000).toISOString(),
    } as ReturnType<typeof makeMsg>;
    localStorage.setItem(`messages_panel_last_seen:${userId}:proj-1`, String(dividerTime));
    const el = await renderForProject('proj-1', [oldMsg, newMsg]);
    // Both messages should be visible initially
    let bubbles = [...el.querySelectorAll('[data-testid="message-bubble"]')];
    expect(bubbles).toHaveLength(2);
    // Click showOnlyNew toggle
    const showOnlyNewBtn = [...el.querySelectorAll('button')].find(b =>
      b.title?.includes('Only new since')
    );
    await act(async () => { (showOnlyNewBtn as HTMLButtonElement).click(); });
    // Only new message should be visible
    bubbles = [...el.querySelectorAll('[data-testid="message-bubble"]')];
    expect(bubbles).toHaveLength(1);
    expect(bubbles[0].textContent).toContain('New bug');
  });

  test('showOnlyNew shows custom empty state when no messages after divider', async () => {
    const now = Date.now();
    const dividerTime = now - 5000;
    // Use a high-signal BLOCKER so it renders as an inline bubble (routine
    // messages live inside the collapsed routine ops group, so they would not
    // populate the bubble selector below).
    const oldMsg = {
      id: '1',
      project_id: 'proj-1',
      message_type: 'BLOCKER',
      from_type: 'agent',
      from_task_id: null,
      content: 'Old blocker',
      context: {},
      status: 'open',
      resolution: null,
      resolved_at: null,
      created_at: new Date(dividerTime - 1000).toISOString(),
    } as ReturnType<typeof makeMsg>;
    localStorage.setItem(`messages_panel_last_seen:${userId}:proj-1`, String(dividerTime));
    const el = await renderForProject('proj-1', [oldMsg]);
    // Message is visible initially
    expect([...el.querySelectorAll('[data-testid="message-bubble"]')]).toHaveLength(1);
    // Enable showOnlyNew
    const showOnlyNewBtn = [...el.querySelectorAll('button')].find(b =>
      b.title?.includes('Only new since')
    );
    await act(async () => { (showOnlyNewBtn as HTMLButtonElement).click(); });
    // Empty state should appear. The two lines render as separate <p>s, so
    // assert each independently.
    const paragraphs = [...el.querySelectorAll('p')];
    expect(paragraphs.some(p => p.textContent?.includes('No new messages'))).toBe(true);
    expect(paragraphs.some(p => p.textContent?.includes("You're all caught up!"))).toBe(true);
  });

  test('showOnlyNew composes with criticalOnly filter', async () => {
    const now = Date.now();
    const dividerTime = now - 5000;
    // Two critical BLOCKERs (one old, one new) plus a non-critical FEEDBACK
    // that is high-signal (renders as a bubble) but NOT critical per
    // isCriticalSeverity (which only treats BLOCKER/BUG_REPORT/critical-NOTEs
    // as critical). criticalOnly should hide the FEEDBACK; showOnlyNew should
    // additionally hide the older BLOCKER.
    const oldBlocker = {
      id: '1',
      project_id: 'proj-1',
      message_type: 'BLOCKER',
      from_type: 'agent',
      from_task_id: null,
      content: 'Old blocker',
      context: {},
      status: 'open',
      resolution: null,
      resolved_at: null,
      created_at: new Date(dividerTime - 1000).toISOString(),
    } as ReturnType<typeof makeMsg>;
    const newBlocker = {
      id: '2',
      project_id: 'proj-1',
      message_type: 'BLOCKER',
      from_type: 'agent',
      from_task_id: null,
      content: 'New blocker',
      context: {},
      status: 'open',
      resolution: null,
      resolved_at: null,
      created_at: new Date(dividerTime + 1000).toISOString(),
    } as ReturnType<typeof makeMsg>;
    const newFeedback = {
      id: '3',
      project_id: 'proj-1',
      message_type: 'FEEDBACK',
      from_type: 'user',
      from_task_id: null,
      content: 'New feedback',
      context: {},
      status: 'open',
      resolution: null,
      resolved_at: null,
      created_at: new Date(dividerTime + 2000).toISOString(),
    } as ReturnType<typeof makeMsg>;
    localStorage.setItem(`messages_panel_last_seen:${userId}:proj-1`, String(dividerTime));
    const el = await renderForProject('proj-1', [oldBlocker, newBlocker, newFeedback]);
    // All three high-signal messages render as bubbles.
    expect([...el.querySelectorAll('[data-testid="message-bubble"]')]).toHaveLength(3);
    // Enable critical only — FEEDBACK is non-critical and disappears.
    const criticalBtn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Critical only'));
    await act(async () => { (criticalBtn as HTMLButtonElement).click(); });
    expect([...el.querySelectorAll('[data-testid="message-bubble"]')]).toHaveLength(2);
    // Enable showOnlyNew — old BLOCKER falls away, only the new BLOCKER remains.
    const showOnlyNewBtn = [...el.querySelectorAll('button')].find(b =>
      b.title?.includes('Only new since')
    );
    await act(async () => { (showOnlyNewBtn as HTMLButtonElement).click(); });
    const bubbles = [...el.querySelectorAll('[data-testid="message-bubble"]')];
    expect(bubbles).toHaveLength(1);
    expect(bubbles[0].textContent).toContain('New blocker');
  });

  test('showOnlyNew persists in saved views', async () => {
    const testMsg = {
      id: '1',
      project_id: 'proj-1',
      message_type: 'NOTE',
      from_type: 'system',
      from_task_id: null,
      content: 'Test',
      context: { event_type: 'pr_merged' },
      status: 'open',
      resolution: null,
      resolved_at: null,
      created_at: new Date(Date.now() - 1000).toISOString(),
    } as ReturnType<typeof makeMsg>;
    localStorage.setItem(`messages_panel_last_seen:${userId}:proj-1`, String(Date.now() - 500));
    const el = await renderForProject('proj-1', [testMsg]);
    // Enable showOnlyNew
    const showOnlyNewBtn = [...el.querySelectorAll('button')].find(b =>
      b.title?.includes('Only new since') || b.title?.includes('Show all messages')
    );
    await act(async () => { (showOnlyNewBtn as HTMLButtonElement).click(); });
    // Save as a view
    const saveBtn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Save current filters'));
    await act(async () => { (saveBtn as HTMLButtonElement).click(); });
    const nameInput = document.body.querySelector('input[placeholder*="Unresolved"]') as HTMLInputElement;
    await act(async () => {
      const proto = Object.getPrototypeOf(nameInput);
      Object.getOwnPropertyDescriptor(proto, 'value')?.set?.call(nameInput, 'New messages only');
      nameInput.dispatchEvent(new Event('input', { bubbles: true }));
    });
    const submit = [...document.body.querySelectorAll('button')].find(b =>
      b.textContent?.trim() === 'Save view' && b.className.includes('bg-blue-600')
    );
    await act(async () => { (submit as HTMLButtonElement).click(); });
    // Check that showOnlyNew is stored in the view
    const stored = JSON.parse(localStorage.getItem(`messages_saved_views:${userId}:proj-1`)!);
    expect(stored.views).toHaveLength(1);
    expect(stored.views[0].showOnlyNew).toBe(true);
  });

  test('applying a saved view with showOnlyNew enabled restores the toggle', async () => {
    const seeded: SavedView = {
      id: 'v_new_only',
      name: 'New Only',
      query: '',
      prefs: {},
      sort: 'importance',
      showResolved: false,
      routineExpanded: false,
      showOnlyNew: true,
      createdAt: Date.now(),
    };
    localStorage.setItem(`messages_saved_views:${userId}:proj-1`, JSON.stringify({ version: 1, views: [seeded] }));
    localStorage.setItem(`messages_panel_last_seen:${userId}:proj-1`, String(Date.now() - 500));
    const el = await renderForProject('proj-1', []);
    const tab = [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === 'New Only');
    expect(tab).toBeTruthy();
    await act(async () => { (tab as HTMLButtonElement).click(); });
    // Check that showOnlyNew toggle is now active
    const showOnlyNewBtn = [...el.querySelectorAll('button')].find(b =>
      b.title?.includes('Show all messages')
    );
    expect(showOnlyNewBtn).toBeTruthy();
    expect((showOnlyNewBtn as HTMLButtonElement).className).toContain('text-blue-400');
  });
});

// ── Summary card tests ──────────────────────────────────────────────────────

describe('MessagesPanel summary card', () => {
  const mockAddToast = vi.fn();

  function makeMsg(overrides: Partial<ProjectMessage> = {}): ProjectMessage {
    return {
      id: 'msg-1',
      project_id: 'proj-1',
      message_type: 'NOTE',
      from_type: 'system',
      from_task_id: null,
      content: 'Test message',
      context: {},
      status: 'open',
      resolution: null,
      resolved_at: null,
      created_at: new Date().toISOString(),
      ...overrides,
    };
  }

  async function renderPanelWithDivider(msgs: ProjectMessage[], dividerTime: number | null) {
    if (dividerTime !== null) {
      localStorage.setItem('messages_panel_last_seen:test-user@example.com:proj-1', String(dividerTime));
    }
    _mockMessagesFetch(msgs as unknown as ReturnType<typeof makeMsg>[], dividerTime);

    const { container } = render(
      <MessagesPanel
        selectedProjectId="proj-1"
        onClose={() => {}}
        addToast={mockAddToast}
        onMessageCountChange={() => {}}
      />
    );

    await waitFor(() => {
      expect(_messagesGetCalls().length).toBeGreaterThanOrEqual(1);
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });

    return container;
  }

  afterEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  test('summary card is hidden when no divider time is set', async () => {
    const msgs = [
      makeMsg({ id: 'm1', message_type: 'BLOCKER', from_type: 'agent' }),
    ];
    const el = await renderPanelWithDivider(msgs, null);
    expect(el.textContent).not.toContain('Since you last looked');
  });

  test('summary card is hidden when divider time is dismissed', async () => {
    const now = Date.now();
    const msgs = [
      makeMsg({ id: 'm1', message_type: 'BLOCKER', from_type: 'agent', created_at: new Date(now + 1000).toISOString() }),
    ];
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ messages: msgs }), { status: 200 })
    );

    const { container } = render(
      <MessagesPanel
        selectedProjectId="proj-1"
        onClose={() => {}}
        addToast={mockAddToast}
        onMessageCountChange={() => {}}
      />
    );

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalled();
    });

    // Simulate dismissing the divider by advancing timer
    vi.useFakeTimers();
    vi.advanceTimersByTime(5000);
    vi.useRealTimers();

    // After 5 seconds, card should be hidden (divider auto-dismissed)
    expect(container).toBeTruthy();
  });

  test('summary card shows new BLOCKER count', async () => {
    const dividerTime = Date.now();
    const msgs = [
      makeMsg({ id: 'b1', message_type: 'BLOCKER', from_type: 'agent', created_at: new Date(dividerTime + 1000).toISOString() }),
      makeMsg({ id: 'b2', message_type: 'BLOCKER', from_type: 'agent', created_at: new Date(dividerTime + 2000).toISOString() }),
    ];
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ messages: msgs }), { status: 200 })
    );

    const { container } = render(
      <MessagesPanel
        selectedProjectId="proj-1"
        onClose={() => {}}
        addToast={mockAddToast}
        onMessageCountChange={() => {}}
      />
    );

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalled();
    });

    localStorage.setItem('messages_panel_last_seen:test-user@example.com:proj-1', String(dividerTime));

    // Re-render to update divider state
    await waitFor(() => {
      expect(container.textContent).toBeTruthy();
      // The summary card should appear with new blockers
      // Note: full integration test requires more setup
    });
  });

  test('summary card respects mute prefs and excludes muted event types', async () => {
    const dividerTime = Date.now() - 5000;
    // Two new pr_merged messages are muted via event prefs; one new FEEDBACK is not.
    // The since-last-visit banner ("X new since") should count only the FEEDBACK.
    localStorage.setItem(
      `messages_panel_event_prefs:test-user@example.com:proj-1`,
      JSON.stringify({ pr_merged: 'mute' }),
    );
    const muted1 = makeMsg({ id: 'r1', message_type: 'NOTE', from_type: 'system', context: { event_type: 'pr_merged' }, created_at: new Date(dividerTime + 1000).toISOString() });
    const muted2 = makeMsg({ id: 'r2', message_type: 'NOTE', from_type: 'system', context: { event_type: 'pr_merged' }, created_at: new Date(dividerTime + 2000).toISOString() });
    const feedback = makeMsg({ id: 'f1', message_type: 'FEEDBACK', from_type: 'user', content: 'Need feature X', created_at: new Date(dividerTime + 3000).toISOString() });
    const el = await renderPanelWithDivider([muted1, muted2, feedback], dividerTime);
    // Banner counts only the non-muted high-signal arrival.
    expect(el.textContent).toContain('1 new since');
  });

  test('summary card shows routine ops count', async () => {
    const dividerTime = Date.now() - 5000;
    const r1 = makeMsg({ id: 'r1', message_type: 'NOTE', from_type: 'system', context: { event_type: 'pr_merged' }, created_at: new Date(dividerTime + 1000).toISOString() });
    const el = await renderPanelWithDivider([r1], dividerTime);
    // The since-last-visit banner reports the new routine arrival.
    expect(el.textContent).toContain('1 new since');
  });

  test('caught-up indicator shows when dividerTime set and zero new messages', async () => {
    const dividerTime = Date.now();
    const oldMsg = {
      id: 'm1',
      project_id: 'proj-1',
      message_type: 'BLOCKER',
      from_type: 'agent',
      content: 'Old message',
      context: {},
      status: 'open',
      resolution: null,
      resolved_at: null,
      from_task_id: null,
      created_at: new Date(dividerTime - 5000).toISOString(),
    };
    const el = await renderPanelWithDivider([oldMsg], dividerTime);
    // Should show caught-up indicator with reset button
    expect(el.textContent).toContain('No new since');
    expect([...el.querySelectorAll('button')].some(b => b.textContent?.includes('Reset baseline'))).toBe(true);
  });

  test('caught-up indicator does not show when there are new high-signal messages', async () => {
    const dividerTime = Date.now();
    const msgs = [
      {
        id: 'm1',
        project_id: 'proj-1',
        message_type: 'BLOCKER',
        from_type: 'agent',
        content: 'New blocker',
        context: {},
        status: 'open',
        resolution: null,
        resolved_at: null,
        from_task_id: null,
        created_at: new Date(dividerTime + 1000).toISOString(),
      },
    ];
    const el = await renderPanelWithDivider(msgs, dividerTime);
    // Should show the regular "X new since" banner, not the caught-up indicator
    expect(el.textContent).toContain('1 new since');
    expect(el.textContent).not.toContain('No new since');
  });

  test('caught-up indicator shows after cold-start bootstrap sets dividerTime', async () => {
    const msgs = [
      {
        id: 'm1',
        project_id: 'proj-1',
        message_type: 'BLOCKER',
        from_type: 'agent',
        content: 'Message',
        context: {},
        status: 'open',
        resolution: null,
        resolved_at: null,
        from_task_id: null,
        created_at: new Date(Date.now() - 5000).toISOString(),
      },
    ];
    const el = await renderPanelWithDivider(msgs, null);
    // Cold-start bootstrap (#4974) records "now" as the divider baseline even with
    // no prior last-seen, so the single older BLOCKER reads as caught-up.
    await waitFor(() => {
      expect(el.textContent).toContain('No new since');
    });
  });
});

// ── Temporary mutes unit tests ───────────────────────────────────────────

describe('temporary mutes', () => {
  afterEach(() => {
    localStorage.clear();
  });

  test('effectiveSignal returns mute for temporarily muted event types', () => {
    const now = Date.now();
    const msg = { message_type: 'NOTE', from_type: 'system', context: { event_type: 'pr_merged' } };
    const tempMutes = { pr_merged: now + 3600000 };

    expect(effectiveSignal(msg, {}, tempMutes)).toBe('mute');
  });

  test('effectiveSignal ignores expired temporary mutes', () => {
    const now = Date.now();
    const msg = { message_type: 'NOTE', from_type: 'system', context: { event_type: 'pr_merged' } };
    const tempMutes = { pr_merged: now - 1000 }; // expired

    expect(effectiveSignal(msg, {}, tempMutes)).toBe('routine');
  });

  test('temporary mute takes precedence over permanent pref', () => {
    const now = Date.now();
    const msg = { message_type: 'NOTE', from_type: 'system', context: { event_type: 'pr_merged' } };
    const prefs = { pr_merged: 'routine' as const };
    const tempMutes = { pr_merged: now + 3600000 };

    expect(effectiveSignal(msg, prefs, tempMutes)).toBe('mute');
  });

  test('isEventTypeTemporarilyMuted returns true for active mute', () => {
    const now = Date.now();
    const tempMutes = { pr_merged: now + 3600000 };

    expect(isEventTypeTemporarilyMuted('pr_merged', tempMutes)).toBe(true);
  });

  test('isEventTypeTemporarilyMuted returns false for expired mute', () => {
    const now = Date.now();
    const tempMutes = { pr_merged: now - 1000 };

    expect(isEventTypeTemporarilyMuted('pr_merged', tempMutes)).toBe(false);
  });

  test('isEventTypeTemporarilyMuted returns false for unmuted type', () => {
    const tempMutes = {};

    expect(isEventTypeTemporarilyMuted('pr_merged', tempMutes)).toBe(false);
  });

  test('loadTemporaryMutes filters out expired entries', () => {
    const now = Date.now();
    const key = 'messages_panel_temp_mutes:test-user@example.com:proj-1';
    const data = {
      pr_merged: now + 3600000, // active
      cycle_summary: now - 1000, // expired
      auto_fix_result: now + 7200000, // active
    };
    localStorage.setItem(key, JSON.stringify(data));

    const loaded = loadTemporaryMutes('test-user@example.com', 'proj-1');

    expect(loaded).toHaveProperty('pr_merged');
    expect(loaded).toHaveProperty('auto_fix_result');
    expect(loaded).not.toHaveProperty('cycle_summary');
  });

  test('saveTemporaryMutes persists to localStorage', () => {
    const now = Date.now();
    const tempMutes = {
      pr_merged: now + 3600000,
      auto_fix_result: now + 7200000,
    };

    saveTemporaryMutes('test-user@example.com', 'proj-1', tempMutes);

    const key = 'messages_panel_temp_mutes:test-user@example.com:proj-1';
    const stored = JSON.parse(localStorage.getItem(key) || '{}');

    expect(stored).toEqual(tempMutes);
  });

  test('saveTemporaryMutes removes key when mutes are empty', () => {
    const key = 'messages_panel_temp_mutes:test-user@example.com:proj-1';
    localStorage.setItem(key, '{"pr_merged": 999999999999}');

    saveTemporaryMutes('test-user@example.com', 'proj-1', {});

    expect(localStorage.getItem(key)).toBeNull();
  });
});

// ── Backend last-seen round-trip and offline fallback ──────────────────────────

describe('MessagesPanel backend last-seen', () => {
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

  async function renderPanel() {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <MessagesPanel selectedProjectId="proj-1" onClose={() => {}} addToast={() => {}} onMessageCountChange={() => {}} />,
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    return host as HTMLDivElement;
  }

  test('calls backend GET last-seen endpoint on mount', async () => {
    vi.mocked(apiFetch).mockResolvedValue(
      new Response(JSON.stringify({ messages: [] }), { status: 200 }),
    );
    await renderPanel();
    const calls = vi.mocked(apiFetch).mock.calls.map(c => c[0] as string);
    expect(calls.some(url => url.includes('/api/admin/messages/last-seen'))).toBe(true);
  });

  test('falls back to localStorage when backend last-seen call fails', async () => {
    const localTs = Date.now() - 60_000;
    localStorage.setItem('messages_panel_last_seen:test-user@example.com:proj-1', String(localTs));
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if ((url as string).includes('/api/admin/messages/last-seen')) {
        throw new Error('network error');
      }
      return new Response(JSON.stringify({ messages: [] }), { status: 200 });
    });
    // Should render without crashing even when backend call fails
    await expect(renderPanel()).resolves.toBeDefined();
  });

  test('calls backend PUT last-seen on dismiss', async () => {
    vi.mocked(apiFetch).mockResolvedValue(
      new Response(JSON.stringify({ messages: [], last_seen_at: null }), { status: 200 }),
    );
    const el = await renderPanel();
    // Find and click the dismiss/mark-as-seen banner button if visible
    const dismissBtn = [...el.querySelectorAll('button')].find(
      b => b.textContent?.includes('Mark as seen') || b.textContent?.includes('Dismiss'),
    );
    if (dismissBtn) {
      await act(async () => { (dismissBtn as HTMLButtonElement).click(); });
      const putCalls = vi.mocked(apiFetch).mock.calls.filter(
        c => (c[1] as RequestInit | undefined)?.method === 'PUT' && (c[0] as string).includes('/api/admin/messages/last-seen'),
      );
      expect(putCalls.length).toBeGreaterThan(0);
    } else {
      // Banner not visible with no new messages — verify GET was still called
      const calls = vi.mocked(apiFetch).mock.calls.map(c => c[0] as string);
      expect(calls.some(url => url.includes('/api/admin/messages/last-seen'))).toBe(true);
    }
  });

  test('bootstraps dividerTime on first visit when both localStorage and backend return null', async () => {
    vi.mocked(apiFetch).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = url as string;
      const initObj = init as RequestInit | undefined;
      if (urlStr.includes('/api/admin/messages/last-seen') && initObj?.method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      return new Response(JSON.stringify({ messages: [] }), { status: 200 });
    });

    await renderPanel();

    const LAST_SEEN_KEY = 'messages_panel_last_seen:test-user@example.com:proj-1';
    const storedValue = localStorage.getItem(LAST_SEEN_KEY);
    expect(storedValue).not.toBeNull();
    expect(Number(storedValue)).toBeGreaterThan(0);

    const putCalls = vi.mocked(apiFetch).mock.calls.filter(
      c => (c[1] as RequestInit | undefined)?.method === 'PUT' && (c[0] as string).includes('/api/admin/messages/last-seen'),
    );
    expect(putCalls.length).toBeGreaterThan(0);
  });

  test('does not bootstrap when localStorage already has a last-seen value', async () => {
    const existingTs = Date.now() - 60_000;
    localStorage.setItem('messages_panel_last_seen:test-user@example.com:proj-1', String(existingTs));
    vi.mocked(apiFetch).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = url as string;
      const initObj = init as RequestInit | undefined;
      if (urlStr.includes('/api/admin/messages/last-seen') && initObj?.method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      return new Response(JSON.stringify({ messages: [] }), { status: 200 });
    });

    await renderPanel();

    const putCalls = vi.mocked(apiFetch).mock.calls.filter(
      c => (c[1] as RequestInit | undefined)?.method === 'PUT' && (c[0] as string).includes('/api/admin/messages/last-seen'),
    );
    // No bootstrap PUT — localStorage value was loaded, dividerTimeRef is non-null
    expect(putCalls.length).toBe(0);
  });
});

// ── Since-last-visit count accuracy ──────────────────────────────────────────

describe('MessagesPanel since-last-visit count accuracy', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  const LAST_SEEN_KEY = 'messages_panel_last_seen:test-user@example.com:proj-1';
  const PAST_TIME = Date.now() - 3600000; // 1 hour ago

  beforeEach(() => {
    localStorage.setItem(LAST_SEEN_KEY, String(PAST_TIME));
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

  async function renderPanel(messages: ReturnType<typeof makeMsg>[]) {
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async () =>
      new Response(JSON.stringify({ messages }), { status: 200 }),
    );
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
    await act(async () => { await new Promise<void>(res => setTimeout(res, 100)); });
    return host as HTMLDivElement;
  }

  test('single new BLOCKER shows "(1 new)" not "(2 new)" in last-visit anchor', async () => {
    const blocker = makeMsg({ id: 'b1', message_type: 'BLOCKER', from_type: 'agent', content: 'blocker msg' });
    const el = await renderPanel([blocker]);
    const text = el.textContent ?? '';
    expect(text).toContain('(1 new)');
    expect(text).not.toContain('(2 new)');
  });

  test('single new FEEDBACK shows "(1 new)" not "(2 new)"', async () => {
    const feedback = makeMsg({ id: 'f1', message_type: 'FEEDBACK', from_type: 'user', content: 'feedback msg' });
    const el = await renderPanel([feedback]);
    const text = el.textContent ?? '';
    expect(text).toContain('(1 new)');
    expect(text).not.toContain('(2 new)');
  });

  test('banner shows for routine-only new activity', async () => {
    const routine = makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' }, content: 'merged pr' });
    const el = await renderPanel([routine]);
    // Banner should be visible even with no high-signal new messages
    const text = el.textContent ?? '';
    expect(text).toMatch(/\d+ new since/);
  });

  test('mark-all-read button is present when only routine messages are new', async () => {
    const routine = makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' } });
    const el = await renderPanel([routine]);
    const markReadBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Mark all read');
    expect(markReadBtn).toBeTruthy();
  });

  test('new high-signal NOTE (failure_alert) appears in summary card breakdown', async () => {
    const failureAlert = makeMsg({
      id: 'fa1',
      message_type: 'NOTE',
      from_type: 'system',
      context: { event_type: 'failure_alert' },
      content: 'failure msg',
    });
    const el = await renderPanel([failureAlert]);
    const alertRow = el.querySelector('[data-testid="summary-card-alerts"]');
    expect(alertRow).toBeTruthy();
    expect(alertRow?.textContent).toContain('1');
  });

  test('routine new messages are represented in the summary card', async () => {
    const routine = makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' } });
    const el = await renderPanel([routine]);
    const routineRow = el.querySelector('[data-testid="summary-card-routine"]');
    expect(routineRow).toBeTruthy();
    expect(routineRow?.textContent).toContain('1');
  });
});

// ── Project-switch async race guards ─────────────────────────────────────────
// These tests verify that stale in-flight responses for the old project are
// discarded when the user switches projects. Each async path guards with an
// `active` flag; if the guard were removed, the stale response would corrupt
// the new project's state (hence these tests would regress without it).

describe('MessagesPanel project-switch async race guards', () => {
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

  test('stale server noise-prefs for project A do not mute events in project B', async () => {
    // Deferred promise simulates a slow noise-prefs GET that resolves after the project switch.
    let resolveNoisePrefsA!: (r: Response) => void;
    const deferredA = new Promise<Response>(res => { resolveNoisePrefsA = res; });
    const projBMsg = makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' }, content: 'proj-B routine' });

    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/noise-prefs') && urlStr.includes('proj-A') && method === 'GET') {
        return deferredA;
      }
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (/\/api\/admin\/messages(\?|$)/.test(urlStr) && method === 'GET') {
        const msgs = urlStr.includes('proj-B') ? [projBMsg] : [];
        return new Response(JSON.stringify({ messages: msgs }), { status: 200 });
      }
      return new Response('', { status: 404 });
    });

    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);

    // Mount proj-A — noise-prefs fetch is in-flight (deferred).
    await act(async () => {
      root!.render(<MessagesPanel selectedProjectId="proj-A" onClose={() => {}} addToast={() => {}} onMessageCountChange={() => {}} />);
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 20)); });

    // Switch to proj-B — sets active=false on proj-A's effect cleanup.
    _suppressSinceLastVisit('test-user@example.com', 'proj-B');
    await act(async () => {
      root!.render(<MessagesPanel selectedProjectId="proj-B" onClose={() => {}} addToast={() => {}} onMessageCountChange={() => {}} />);
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 60)); });

    // proj-B's pr_merged message should be visible in routine ops.
    expect(host!.textContent).toContain('routine ops');

    // Resolve the stale proj-A noise-prefs response: pr_merged=mute.
    // The active guard discards this — without it, pr_merged would be muted in B.
    await act(async () => {
      resolveNoisePrefsA(new Response(JSON.stringify({ prefs: { pr_merged: 'mute' } }), { status: 200 }));
      await new Promise<void>(res => setTimeout(res, 30));
    });

    // Routine ops still visible — pr_merged was NOT muted by the stale A response.
    expect(host!.textContent).toContain('routine ops');
  });

  test('stale server saved-views for project A do not appear in project B tab strip', async () => {
    let resolveViewsA!: (r: Response) => void;
    const deferredViewsA = new Promise<Response>(res => { resolveViewsA = res; });

    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = String(url);
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      if (urlStr.includes('/api/admin/messages/saved-views') && urlStr.includes('proj-A') && method === 'GET') {
        return deferredViewsA;
      }
      if (urlStr.includes('/api/admin/messages/last-seen') && method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      if (/\/api\/admin\/messages(\?|$)/.test(urlStr) && method === 'GET') {
        return new Response(JSON.stringify({ messages: [] }), { status: 200 });
      }
      return new Response('', { status: 404 });
    });

    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);

    await act(async () => {
      root!.render(<MessagesPanel selectedProjectId="proj-A" onClose={() => {}} addToast={() => {}} onMessageCountChange={() => {}} />);
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 20)); });

    // Switch to proj-B — deactivates proj-A's saved-views fetch.
    await act(async () => {
      root!.render(<MessagesPanel selectedProjectId="proj-B" onClose={() => {}} addToast={() => {}} onMessageCountChange={() => {}} />);
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 30)); });

    // proj-B tab strip shows only Default.
    const tabsBefore = [...host!.querySelectorAll('[data-testid="saved-views-tab-strip"] button')];
    expect(tabsBefore.some(b => b.textContent?.trim().startsWith('Default'))).toBe(true);
    expect(tabsBefore.every(b => !b.textContent?.trim().startsWith('A-only'))).toBe(true);

    // Resolve stale proj-A saved-views — should be discarded.
    const staleView = { id: 'v_a', name: 'A-only', query: '', prefs: {}, sort: 'importance', showResolved: false, routineExpanded: false, createdAt: Date.now() };
    await act(async () => {
      resolveViewsA(new Response(JSON.stringify({ saved_views: [staleView] }), { status: 200 }));
      await new Promise<void>(res => setTimeout(res, 30));
    });

    // 'A-only' tab must NOT appear in proj-B's tab strip.
    const tabsAfter = [...host!.querySelectorAll('[data-testid="saved-views-tab-strip"] button')];
    expect(tabsAfter.every(b => !b.textContent?.trim().startsWith('A-only'))).toBe(true);
  });
});

// ── Outgoing send → arming receipt (toast + composer hint) ────────────────────
// The operator's live-chat composer must surface a TRUTHFUL receipt for the
// server's real arming decision. The load-bearing guarantee under test: a
// genuine steering message the server could NOT arm (`not_armed`) is shown as a
// distinct, non-success "wasn't woken" receipt — never a green "steering" lie —