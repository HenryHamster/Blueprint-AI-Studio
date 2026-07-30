import { describe, test, expect, vi, afterEach, beforeEach } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { render, waitFor } from '@testing-library/react';
import { MessagesPanel } from './MessagesPanel';
import { effectiveSignal, loadEventPrefs, saveEventPrefs, EVENT_PREFS_KEY, eventPrefsKey, isCriticalSeverity } from './MessagesPanel.utils';
import type { ProjectMessage } from './MessageBubble';
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


// ── Toast notification tests ─────────────────────────────────────────────────

describe('MessagesPanel toast notifications', () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  test('does not fire toast on initial load with BLOCKER messages', async () => {
    const mockAddToast = vi.fn();
    const blocker = {
      id: 'msg-1',
      project_id: 'proj-1',
      message_type: 'BLOCKER',
      from_type: 'agent',
      from_task_id: 'task-1',
      content: 'System is blocked',
      context: {},
      status: 'open',
      resolution: null,
      resolved_at: null,
      created_at: '2026-05-06T10:00:00Z',
    };

    _mockMessagesFetch([blocker]);

    render(
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

    // Toast should NOT fire on initial load
    expect(mockAddToast).not.toHaveBeenCalled();
  });

  test('fires toast when new BLOCKER arrives on subsequent poll', async () => {
    vi.useFakeTimers();
    try {
      const mockAddToast = vi.fn();
      const blocker1 = {
        id: 'msg-1',
        project_id: 'proj-1',
        message_type: 'BLOCKER',
        from_type: 'agent',
        from_task_id: 'task-1',
        content: 'First blocker',
        context: {},
        status: 'open',
        resolution: null,
        resolved_at: null,
        created_at: '2026-05-06T10:00:00Z',
      };
      const blocker2 = {
        ...blocker1,
        id: 'msg-2',
        content: 'Second blocker arrived later',
      };

      // First fetch returns one BLOCKER (initial load — should NOT toast).
      // Second fetch (poll) returns the original plus a fresh BLOCKER —
      // toast SHOULD fire exactly once for the new one.
      _mockMessagesFetchSequence([[blocker1], [blocker1, blocker2]]);

      render(
        <MessagesPanel
          selectedProjectId="proj-1"
          onClose={() => {}}
          addToast={mockAddToast}
          onMessageCountChange={() => {}}
          strategicState={strategicStateForActionMessage('msg-2')}
        />
      );

      // Initial load completes — no toast yet.
      await vi.waitFor(() => {
        expect(_messagesGetCalls().length).toBeGreaterThanOrEqual(1);
      });
      expect(mockAddToast).not.toHaveBeenCalled();

      // Advance the 30s polling interval to trigger the second fetch.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });

      await vi.waitFor(() => {
        expect(_messagesGetCalls().length).toBeGreaterThanOrEqual(2);
      });
      await vi.waitFor(() => {
        expect(mockAddToast).toHaveBeenCalledTimes(1);
      });
      const [summary, level] = mockAddToast.mock.calls[0];
      expect(summary).toContain('needs-action message');
      expect(summary).toContain('Second blocker arrived later');
      expect(level).toBe('error');
    } finally {
      vi.useRealTimers();
    }
  });

  test('resets toast suppression when selectedProjectId changes', async () => {
    const mockAddToast = vi.fn();
    const projAblocker = {
      id: 'a-1',
      project_id: 'proj-A',
      message_type: 'BLOCKER',
      from_type: 'agent',
      from_task_id: 't1',
      content: 'A blocker',
      context: {},
      status: 'open',
      resolution: null,
      resolved_at: null,
      created_at: '2026-05-06T10:00:00Z',
    };
    const projBblocker = {
      ...projAblocker,
      id: 'b-1',
      project_id: 'proj-B',
      content: 'B blocker',
    };

    // First fetch (project A initial load) — no toast.
    _mockMessagesFetch([projAblocker]);

    const { rerender } = render(
      <MessagesPanel
        selectedProjectId="proj-A"
        onClose={() => {}}
        addToast={mockAddToast}
        onMessageCountChange={() => {}}
      />
    );

    await waitFor(() => {
      expect(_messagesGetCalls().length).toBeGreaterThanOrEqual(1);
    });
    expect(mockAddToast).not.toHaveBeenCalled();

    // Switch mock to project B BEFORE rerender so the next messages fetch sees
    // B's pre-existing BLOCKER as part of B's initial load, not a poll arrival.
    const callsBefore = _messagesGetCalls().length;
    _mockMessagesFetch([projBblocker]);

    rerender(
      <MessagesPanel
        selectedProjectId="proj-B"
        onClose={() => {}}
        addToast={mockAddToast}
        onMessageCountChange={() => {}}
      />
    );

    await waitFor(() => {
      expect(_messagesGetCalls().length).toBeGreaterThan(callsBefore);
    });

    // Project B's pre-existing open BLOCKER is treated as an initial load
    // for B, so no spurious toast fires.
    expect(mockAddToast).not.toHaveBeenCalled();
  });

  test('uses the desktop preference only to present canonical needs_operator attention', async () => {
    vi.useFakeTimers();
    const originalNotification = window.Notification;
    const desktopNotification = vi.fn();
    Object.assign(desktopNotification, { permission: 'granted' });
    Object.defineProperty(window, 'Notification', { configurable: true, value: desktopNotification });
    try {
      const actionMessage = {
        id: 'action-1', project_id: 'proj-1', message_type: 'NOTE', from_type: 'system',
        from_task_id: null, content: 'A decision needs your review',
        context: { audience: 'operator-action-needed' }, status: 'open', resolution: null,
        resolved_at: null, created_at: '2026-05-06T10:00:00Z',
      };
      const addToast = vi.fn();
      _mockMessagesFetchSequence([[], [actionMessage]]);

      render(<MessagesPanel
        selectedProjectId="proj-1" onClose={() => {}} addToast={addToast}
        onMessageCountChange={() => {}} notifyOnHighSignalMessage
        strategicState={strategicStateForActionMessage('action-1')}
      />);

      await vi.waitFor(() => expect(_messagesGetCalls().length).toBeGreaterThanOrEqual(1));
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
      await vi.waitFor(() => expect(addToast).toHaveBeenCalledTimes(1));
      expect(desktopNotification).toHaveBeenCalledWith('NOTE', {
        body: 'A decision needs your review',
      });
    } finally {
      Object.defineProperty(window, 'Notification', { configurable: true, value: originalNotification });
      vi.useRealTimers();
    }
  });

  test('does not fire toast for routine NOTE messages on initial load', async () => {
    const mockAddToast = vi.fn();
    const note = {
      id: 'msg-1',
      project_id: 'proj-1',
      message_type: 'NOTE',
      from_type: 'system',
      from_task_id: null,
      content: 'Routine operation completed',
      context: { event_type: 'pr_merged' },
      status: 'open',
      resolution: null,
      resolved_at: null,
      created_at: '2026-05-06T10:00:00Z',
    };

    _mockMessagesFetch([note]);

    render(
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

    // Routine NOTE messages do not fire toast
    expect(mockAddToast).not.toHaveBeenCalled();
  });

  test('does not promote operator-FYI high-signal NOTE into a toast', async () => {
    vi.useFakeTimers();
    try {
      const mockAddToast = vi.fn();
      const failureAlert = {
        id: 'msg-1',
        project_id: 'proj-1',
        message_type: 'NOTE',
        from_type: 'system',
        from_task_id: null,
        content: 'Pipeline failure detected',
        context: { event_type: 'failure_alert' },
        status: 'open',
        resolution: null,
        resolved_at: null,
        created_at: '2026-05-06T10:00:00Z',
      };

      // This remains a locally high-signal event but lacks a canonical
      // needs_operator attention item, so it must stay non-intrusive.
      _mockMessagesFetchSequence([[], [failureAlert]]);

      render(
        <MessagesPanel
          selectedProjectId="proj-1"
          onClose={() => {}}
          addToast={mockAddToast}
          onMessageCountChange={() => {}}
        />
      );

      // Initial load completes — no toast yet.
      await vi.waitFor(() => {
        expect(_messagesGetCalls().length).toBeGreaterThanOrEqual(1);
      });
      expect(mockAddToast).not.toHaveBeenCalled();

      // Advance the 30s polling interval to trigger the second fetch.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });

      await vi.waitFor(() => {
        expect(_messagesGetCalls().length).toBeGreaterThanOrEqual(2);
      });
      expect(mockAddToast).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  test('fires toast for needs_operator message even when event_type is muted', async () => {
    vi.useFakeTimers();
    try {
      const mockAddToast = vi.fn();
      const failureAlert = {
        id: 'msg-1',
        project_id: 'proj-1',
        message_type: 'NOTE',
        from_type: 'system',
        from_task_id: null,
        content: 'Pipeline failure detected',
        context: { event_type: 'failure_alert' },
        status: 'open',
        resolution: null,
        resolved_at: null,
        created_at: '2026-05-06T10:00:00Z',
      };

      // Mute the event_type locally — the attention model must override this
      // for needs_operator items so all four notification surfaces stay consistent.
      saveEventPrefs('test-user@example.com', 'proj-1', { failure_alert: 'mute' });

      // Initial load empty, then poll returns the failure_alert which is in
      // the attention model as needs_operator.
      _mockMessagesFetchSequence([[], [failureAlert]]);

      render(
        <MessagesPanel
          selectedProjectId="proj-1"
          onClose={() => {}}
          addToast={mockAddToast}
          onMessageCountChange={() => {}}
          strategicState={strategicStateForActionMessage('msg-1')}
        />
      );

      await vi.waitFor(() => {
        expect(_messagesGetCalls().length).toBeGreaterThanOrEqual(1);
      });

      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });

      await vi.waitFor(() => {
        expect(_messagesGetCalls().length).toBeGreaterThanOrEqual(2);
      });

      // Attention model (needs_operator) overrides local mute prefs for toasts —
      // the four notification surfaces must agree on the canonical server signal.
      await vi.waitFor(() => {
        expect(mockAddToast).toHaveBeenCalledTimes(1);
      });
      expect(mockAddToast.mock.calls[0][0]).toContain('needs-action message');
    } finally {
      vi.useRealTimers();
      localStorage.removeItem(eventPrefsKey('test-user@example.com', 'proj-1'));
    }
  });

  test('does not promote strategic-internal or FYI feedback into a toast', async () => {
    vi.useFakeTimers();
    try {
      const mockAddToast = vi.fn();
      const feedback = {
        id: 'msg-1',
        project_id: 'proj-1',
        message_type: 'FEEDBACK',
        from_type: 'system',
        from_task_id: null,
        content: 'Consider adding validation',
        context: { audience: 'operator-fyi' },
        status: 'open',
        resolution: null,
        resolved_at: null,
        created_at: '2026-05-06T10:00:00Z',
      };

      _mockMessagesFetchSequence([[], [feedback]]);

      render(
        <MessagesPanel
          selectedProjectId="proj-1"
          onClose={() => {}}
          addToast={mockAddToast}
          onMessageCountChange={() => {}}
        />
      );

      await vi.waitFor(() => {
        expect(_messagesGetCalls().length).toBeGreaterThanOrEqual(1);
      });

      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });

      expect(mockAddToast).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});

// ── effectiveSignal unit tests ────────────────────────────────────────────────

describe('effectiveSignal', () => {
  const base = { message_type: 'NOTE', from_type: 'system', context: {} };
  const routineMsg = { ...base, context: { event_type: 'pr_merged' } };

  test('mute pref hides message regardless of default classification', () => {
    expect(effectiveSignal(routineMsg, { pr_merged: 'mute' })).toBe('mute');
  });

  test('surfaced pref promotes routine event to high-signal', () => {
    expect(effectiveSignal(routineMsg, { pr_merged: 'surfaced' })).toBe('high');
  });

  test('explicit routine pref falls through to classifySignalLevel', () => {
    expect(effectiveSignal(routineMsg, { pr_merged: 'routine' })).toBe('routine');
  });

  test('no pref falls through to classifySignalLevel (routine by default)', () => {
    expect(effectiveSignal(routineMsg, {})).toBe('routine');
  });

  test('mute pref on a high-signal message type still returns mute', () => {
    const blockerMsg = { message_type: 'BLOCKER', from_type: 'agent', context: { event_type: 'blocker_event' } };
    expect(effectiveSignal(blockerMsg, { blocker_event: 'mute' })).toBe('mute');
  });

  test('pref key must match event_type in context — unmatched pref is ignored', () => {
    expect(effectiveSignal(routineMsg, { some_other_event: 'mute' })).toBe('routine');
  });

  test('message with no event_type in context ignores prefs', () => {
    const noEtMsg = { ...base, context: {} };
    expect(effectiveSignal(noEtMsg, { pr_merged: 'mute' })).toBe('routine');
  });

  test('routine pref demotes failure_alert from high-signal to routine', () => {
    const failureMsg = { ...base, context: { event_type: 'failure_alert' } };
    expect(effectiveSignal(failureMsg, { failure_alert: 'routine' })).toBe('routine');
  });

  test('routine pref demotes account_model_regression_quarantined from high to routine', () => {
    const quarantineMsg = { ...base, context: { event_type: 'account_model_regression_quarantined' } };
    expect(effectiveSignal(quarantineMsg, { account_model_regression_quarantined: 'routine' })).toBe('routine');
  });

  test('routine pref demotes auto_fix_escalated from high to routine', () => {
    const escalatedMsg = { ...base, context: { event_type: 'auto_fix_escalated' } };
    expect(effectiveSignal(escalatedMsg, { auto_fix_escalated: 'routine' })).toBe('routine');
  });

  test('routine pref demotes remediation_escalated from high to routine', () => {
    const remediationMsg = { ...base, context: { event_type: 'remediation_escalated' } };
    expect(effectiveSignal(remediationMsg, { remediation_escalated: 'routine' })).toBe('routine');
  });

  test('routine pref demotes residual_fix_escalated from high to routine', () => {
    const residualMsg = { ...base, context: { event_type: 'residual_fix_escalated' } };
    expect(effectiveSignal(residualMsg, { residual_fix_escalated: 'routine' })).toBe('routine');
  });

  test('routine pref demotes residual_fix_audit_deferred from high to routine', () => {
    const auditMsg = { ...base, context: { event_type: 'residual_fix_audit_deferred' } };
    expect(effectiveSignal(auditMsg, { residual_fix_audit_deferred: 'routine' })).toBe('routine');
  });

  test('high-signal message with no pref stays high by default', () => {
    const failureMsg = { ...base, context: { event_type: 'failure_alert' } };
    expect(effectiveSignal(failureMsg, {})).toBe('high');
  });
});

// ── loadEventPrefs / localStorage persistence tests ───────────────────────────

describe('loadEventPrefs', () => {
  afterEach(() => localStorage.removeItem(EVENT_PREFS_KEY));

  test('returns empty object when nothing in localStorage', () => {
    expect(loadEventPrefs()).toEqual({});
  });

  test('returns parsed prefs from localStorage', () => {
    localStorage.setItem(EVENT_PREFS_KEY, JSON.stringify({ pr_merged: 'mute', cycle_summary: 'surfaced' }));
    expect(loadEventPrefs()).toEqual({ pr_merged: 'mute', cycle_summary: 'surfaced' });
  });

  test('returns empty object on malformed JSON', () => {
    localStorage.setItem(EVENT_PREFS_KEY, 'not-json{{{');
    expect(loadEventPrefs()).toEqual({});
  });

  test('preferences survive a simulated page reload (re-read from localStorage)', () => {
    localStorage.setItem(EVENT_PREFS_KEY, JSON.stringify({ auto_fix_spawned: 'mute' }));
    // Simulates a new page load calling loadEventPrefs() fresh
    const loaded = loadEventPrefs();
    expect(loaded.auto_fix_spawned).toBe('mute');
  });
});

// ── Time-window mute component tests ─────────────────────────────────────────

describe('MessagesPanel mute window', () => {
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
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    _mockMessagesFetch(messages);
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

  test('mute dropdown renders 1h/4h/8h options', async () => {
    const el = await renderPanel([]);
    const muteBtn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Mute routine'));
    expect(muteBtn).toBeTruthy();
    await act(async () => { (muteBtn as HTMLButtonElement).click(); });
    expect(el.textContent).toContain('1 hour');
    expect(el.textContent).toContain('4 hours');
    expect(el.textContent).toContain('8 hours');
  });

  test('selecting 1h surfaces the mute-active banner on the routine section', async () => {
    const msgs = [makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' } })];
    const el = await renderPanel(msgs);
    expect(el.textContent).toContain('routine ops');
    expect(el.textContent).not.toContain('Mute active');

    const muteBtn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Mute routine'));
    await act(async () => { (muteBtn as HTMLButtonElement)?.click(); });
    const oneHourBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === '1 hour');
    await act(async () => { (oneHourBtn as HTMLButtonElement)?.click(); });

    // The routine ops section header remains visible but is annotated with the
    // mute-active banner; muted routine messages are filtered from the expanded
    // contents (only failing/promoted groups still surface).
    expect(el.textContent).toContain('Mute active');
  });

  test('countdown chip shows amber styling when active', async () => {
    const el = await renderPanel([]);
    const muteBtn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Mute routine'));
    await act(async () => { (muteBtn as HTMLButtonElement)?.click(); });
    const oneHourBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === '1 hour');
    await act(async () => { (oneHourBtn as HTMLButtonElement)?.click(); });

    const chip = [...el.querySelectorAll('button')].find(b => b.textContent?.includes('left'));
    expect(chip).toBeTruthy();
    expect(chip?.className).toContain('amber');
  });

  test('clicking End mute early clears window and removes the mute banner', async () => {
    const msgs = [makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' } })];
    const el = await renderPanel(msgs);

    const muteBtn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Mute routine'));
    await act(async () => { (muteBtn as HTMLButtonElement)?.click(); });
    const oneHourBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === '1 hour');
    await act(async () => { (oneHourBtn as HTMLButtonElement)?.click(); });
    expect(el.textContent).toContain('Mute active');

    const chip = [...el.querySelectorAll('button')].find(b => b.textContent?.includes('left'));
    await act(async () => { (chip as HTMLButtonElement)?.click(); });
    const endBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.includes('End mute'));
    await act(async () => { (endBtn as HTMLButtonElement)?.click(); });

    expect(el.textContent).toContain('routine ops');
    expect(el.textContent).not.toContain('Mute active');
  });

  test('BLOCKERs remain visible while mute is active', async () => {
    const msgs = [
      makeMsg({ id: 'b1', message_type: 'BLOCKER', from_type: 'agent', content: 'critical blocker' }),
      makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' }, content: 'routine msg' }),
    ];
    const el = await renderPanel(msgs);

    const muteBtn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Mute routine'));
    await act(async () => { (muteBtn as HTMLButtonElement)?.click(); });
    const oneHourBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === '1 hour');
    await act(async () => { (oneHourBtn as HTMLButtonElement)?.click(); });

    // BLOCKERs are never muted; the mute banner annotates the routine section
    // but the critical blocker content remains visible.
    await waitFor(() => {
      expect(el.textContent).toContain('critical blocker');
      expect(el.textContent).toContain('Mute active');
    });
  });

  test('mute window persists across re-renders via localStorage', async () => {
    const expiresAt = Date.now() + 3600000;
    localStorage.setItem('messages_panel_mute_window:test-user@example.com:proj-1', JSON.stringify({ expiresAt }));
    const msgs = [makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' } })];
    const el = await renderPanel(msgs);
    // The mute banner appears because the persisted window was loaded on mount.
    expect(el.textContent).toContain('Mute active');
  });
});

// ── Per-event-type mute/surface component integration tests ───────────────────

describe('MessagesPanel per-event-type prefs', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  // The panel reads prefs via loadEventPrefs(auth.currentUser.email, selectedProjectId),
  // i.e. the project-scoped key — not the legacy flat key. Seed/clear through the
  // same (email, projectId) the harness renders with so the real scoped read path
  // is exercised end-to-end.
  const PREFS_USER = 'test-user@example.com';
  const PREFS_PROJECT = 'proj-1';
  const scopedPrefsKey = eventPrefsKey(PREFS_USER, PREFS_PROJECT);

  beforeEach(() => localStorage.removeItem(scopedPrefsKey));

  afterEach(() => {
    const r = root;
    if (r) act(() => r.unmount());
    root = null;
    host?.remove();
    host = null;
    localStorage.removeItem(scopedPrefsKey);
    vi.clearAllMocks();
  });

  async function renderPanel(messages: ReturnType<typeof makeMsg>[]) {
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
    return host!;
  }

  test('muted event_type is hidden from the routine group', async () => {
    saveEventPrefs(PREFS_USER, PREFS_PROJECT, { pr_merged: 'mute' });
    const msgs = [makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' }, content: 'muted content' })];
    const el = await renderPanel(msgs);
    expect(el.textContent).not.toContain('muted content');
    expect(el.textContent).not.toContain('routine ops');
  });

  test('muted event_type is hidden from the high-signal area too', async () => {
    // Even a BLOCKER can be muted via explicit pref
    saveEventPrefs(PREFS_USER, PREFS_PROJECT, { blocker_ev: 'mute' });
    const msgs = [
      makeMsg({ id: 'b1', message_type: 'BLOCKER', from_type: 'agent', context: { event_type: 'blocker_ev' }, content: 'muted blocker' }),
    ];
    const el = await renderPanel(msgs);
    expect(el.textContent).not.toContain('muted blocker');
  });

  test('surfaced event_type appears in the high-signal area, not in routine group', async () => {
    saveEventPrefs(PREFS_USER, PREFS_PROJECT, { pr_merged: 'surfaced' });
    const msgs = [makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' }, content: 'promoted content' })];
    const el = await renderPanel(msgs);
    // content is visible
    expect(el.textContent).toContain('promoted content');
    // routine ops group is absent (no routine messages remain)
    expect(el.textContent).not.toContain('routine ops');
  });

  test('clearing all preferences returns to default classification', async () => {
    // Start with pr_merged surfaced (in the project-scoped store) so it appears as high-signal
    saveEventPrefs(PREFS_USER, PREFS_PROJECT, { pr_merged: 'surfaced' });
    const msgs = [makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' }, content: 'pref msg' })];
    const el = await renderPanel(msgs);
    // With surfaced pref, content is directly visible (high-signal)
    expect(el.textContent).toContain('pref msg');
    expect(el.textContent).not.toContain('routine ops');

    // Simulate "Reset all" — clear the scoped store via saveEventPrefs({}) (the
    // panel's own reset removes the project-scoped key when prefs are emptied)
    saveEventPrefs(PREFS_USER, PREFS_PROJECT, {});
    // The scoped read now returns {} (no migration fallback was ever seeded)
    expect(loadEventPrefs(PREFS_USER, PREFS_PROJECT)).toEqual({});
    // effectiveSignal falls back to classifySignalLevel — pr_merged is routine
    const msg = { message_type: 'NOTE', from_type: 'system', context: { event_type: 'pr_merged' } };
    const { effectiveSignal: es } = await import('./MessagesPanel.utils');
    expect(es(msg, {})).toBe('routine');
  });
});

describe('MessagesPanel event type volume in prefs popover', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) act(() => r.unmount());
    root = null;
    host?.remove();
    host = null;
    sessionStorage.clear();
    vi.clearAllMocks();
  });

  async function renderPanel(messages: ReturnType<typeof makeMsg>[]) {
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
    await act(async () => { await new Promise<void>(res => setTimeout(res, 100)); });
    return host as HTMLDivElement;
  }

  test('renders event type count in prefs popover', async () => {
    const msgs = [
      makeMsg({ id: 'm1', from_type: 'system', context: { event_type: 'pr_merged' } }),
      makeMsg({ id: 'm2', from_type: 'system', context: { event_type: 'pr_merged' } }),
      makeMsg({ id: 'm3', from_type: 'system', context: { event_type: 'auto_fix_spawned' } }),
    ];
    const el = await renderPanel(msgs);

    // Open the prefs popover
    const settingsBtn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Event type visibility'));
    expect(settingsBtn).toBeTruthy();
    await act(async () => { (settingsBtn as HTMLButtonElement).click(); });

    const popover = el.querySelector('[class*="bg-neutral-900"][class*="border-neutral-700"][class*="rounded-xl"]');
    expect(popover).toBeTruthy();
    const popoverText = popover?.textContent ?? '';
    // Popover should list event types with spaces (pr merged, auto fix spawned)
    expect(popoverText).toContain('pr merged');
    expect(popoverText).toContain('auto fix spawned');
  });

  test('sorts event types by 24h volume descending in prefs popover', async () => {
    const msgs = [
      ...Array.from({ length: 5 }, (_, i) => makeMsg({ id: `m${i}`, from_type: 'system', context: { event_type: 'pr_merged' } })),
      ...Array.from({ length: 2 }, (_, i) => makeMsg({ id: `a${i}`, from_type: 'system', context: { event_type: 'auto_fix_spawned' } })),
    ];
    const el = await renderPanel(msgs);

    // Open the prefs popover
    const settingsBtn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Event type visibility'));
    await act(async () => { (settingsBtn as HTMLButtonElement).click(); });

    // pr merged (5 events) should appear before auto fix spawned (2 events) when sorted by count
    const popover = el.querySelector('[class*="bg-neutral-900"][class*="border-neutral-700"][class*="rounded-xl"]');
    const popoverText = popover?.textContent ?? '';
    const prMergedIdx = popoverText.indexOf('pr merged');
    const autoFixIdx = popoverText.indexOf('auto fix spawned');
    expect(prMergedIdx).toBeLessThan(autoFixIdx);
    expect(prMergedIdx).toBeGreaterThanOrEqual(0);
  });

  test('highlights high-volume events (10+) with amber styling', async () => {
    const msgs = Array.from({ length: 12 }, (_, i) =>
      makeMsg({ id: `m${i}`, from_type: 'system', context: { event_type: 'pr_merged' } }),
    );
    const el = await renderPanel(msgs);

    // Open the prefs popover
    const settingsBtn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Event type visibility'));
    await act(async () => { (settingsBtn as HTMLButtonElement).click(); });

    // Find the count badge - it's the span within the popover row that shows "12"
    const popover = el.querySelector('[class*="bg-neutral-900"][class*="border-neutral-700"][class*="rounded-xl"]');
    const countBadges = popover?.querySelectorAll('span');
    const countBadge = countBadges && [...countBadges].find(s => s.textContent?.trim() === '12');
    expect(countBadge).toBeTruthy();
    // Check for orange/amber styling
    expect(countBadge?.className).toContain('orange');
  });

  test('excludes messages older than 24 hours from volume count', async () => {
    const now = new Date();
    const yesterday = new Date(now.getTime() - 48 * 60 * 60 * 1000); // 48 hours ago
    const msgs = [
      makeMsg({ id: 'm1', from_type: 'system', context: { event_type: 'pr_merged' }, created_at: now.toISOString() }),
      makeMsg({ id: 'm2', from_type: 'system', context: { event_type: 'pr_merged' }, created_at: yesterday.toISOString() }),
    ];
    const el = await renderPanel(msgs);

    // Open the prefs popover
    const settingsBtn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Event type visibility'));
    await act(async () => { (settingsBtn as HTMLButtonElement).click(); });

    const popover = el.querySelector('[class*="bg-neutral-900"][class*="border-neutral-700"][class*="rounded-xl"]');
    // Should only count the recent one (1), not both (2)
    // Check that the popover contains the count 1 for pr merged
    const countBadges = popover?.querySelectorAll('span');
    const countBadge = countBadges && [...countBadges].find(s => s.textContent?.trim() === '1');
    expect(countBadge).toBeTruthy();
  });
});

describe('MessagesPanel persistence', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  const key = 'messages_panel_prefs:test-user@example.com';

  beforeEach(() => {
    localStorage.removeItem(key);
  });

  afterEach(() => {
    const r = root;
    if (r) act(() => r.unmount());
    root = null;
    host?.remove();
    host = null;
    localStorage.removeItem(key);
    vi.clearAllMocks();
  });

  const routineMsg = makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' } });

  async function renderPanel(messages = [routineMsg]) {
    _suppressSinceLastVisit('test-user@example.com', 'proj-1');
    _mockMessagesFetch(messages);
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root!.render(
        <MessagesPanel selectedProjectId="proj-1" onClose={() => {}} addToast={() => {}} onMessageCountChange={() => {}} />,
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
  }

  test('persists routineExpanded and falls back to defaults', async () => {
    const clickRoutine = () => [...host!.querySelectorAll('button')].find(b => b.textContent?.includes('routine ops'))?.click();
    await renderPanel(); await act(async () => clickRoutine()); expect(JSON.parse(localStorage.getItem(key) || '{}')).toMatchObject({ routineExpanded: true });
    await act(async () => { root!.unmount(); }); root = null; await renderPanel(); expect(host!.textContent).toContain('test content');
    localStorage.removeItem(key); await act(async () => { root!.unmount(); }); root = null; await renderPanel(); expect(JSON.parse(localStorage.getItem(key) || '{}')).toMatchObject({ routineExpanded: false, showResolved: false });
    localStorage.setItem(key, '}{'); await act(async () => { root!.unmount(); }); root = null; await expect(renderPanel()).resolves.toBeUndefined(); expect(JSON.parse(localStorage.getItem(key) || '{}')).toMatchObject({ routineExpanded: false, showResolved: false });
  });
});

describe('isCriticalSeverity', () => {
  test('returns true for BLOCKER', () => {
    expect(isCriticalSeverity({ message_type: 'BLOCKER', context: {} })).toBe(true);
  });

  test('returns true for BUG_REPORT', () => {
    expect(isCriticalSeverity({ message_type: 'BUG_REPORT', context: {} })).toBe(true);
  });

  test('returns false for FEEDBACK', () => {
    expect(isCriticalSeverity({ message_type: 'FEEDBACK', context: {} })).toBe(false);
  });

  test('returns true for QUESTION', () => {
    expect(isCriticalSeverity({ message_type: 'QUESTION', context: {} })).toBe(true);
  });

  test('returns true for NOTE with failure_alert event_type', () => {
    expect(isCriticalSeverity({ message_type: 'NOTE', context: { event_type: 'failure_alert' } })).toBe(true);
  });

  test('returns true for NOTE with rollback_recommended event_type', () => {
    expect(isCriticalSeverity({ message_type: 'NOTE', context: { event_type: 'rollback_recommended' } })).toBe(true);
  });

  test('returns true for NOTE with fix_loop_divergence_regressed event_type', () => {
    expect(isCriticalSeverity({ message_type: 'NOTE', context: { event_type: 'fix_loop_divergence_regressed' } })).toBe(true);
  });

  test('returns false for NOTE with account_model_regression_quarantined event_type', () => {
    // Single quarantine is not critical; clusters of quarantines with high failure rate are promoted via isFailingGroup
    expect(isCriticalSeverity({ message_type: 'NOTE', context: { event_type: 'account_model_regression_quarantined' } })).toBe(false);
  });

  test('returns true for NOTE with auto_fix_escalated event_type', () => {
    expect(isCriticalSeverity({ message_type: 'NOTE', context: { event_type: 'auto_fix_escalated' } })).toBe(true);
  });

  test('returns false for NOTE with pr_merged event_type', () => {
    expect(isCriticalSeverity({ message_type: 'NOTE', context: { event_type: 'pr_merged' } })).toBe(false);
  });

  test('returns false for NOTE with no event_type', () => {
    expect(isCriticalSeverity({ message_type: 'NOTE', context: {} })).toBe(false);
  });
});

describe('MessagesPanel critical-only severity filter', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  const key = 'messages_panel_prefs:test-user@example.com';

  beforeEach(() => localStorage.removeItem(key));

  afterEach(() => {
    const r = root;
    if (r) act(() => r.unmount());
    root = null;
    host?.remove();
    host = null;
    localStorage.removeItem(key);
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

  test('severity filter button is present in header', async () => {
    const blocker = makeMsg({ id: 'b1', message_type: 'BLOCKER', from_type: 'agent' });
    const el = await renderPanel([blocker]);
    const btn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Critical only'));
    expect(btn).toBeTruthy();
  });

  test('critical-only filter hides FEEDBACK when active', async () => {
    const blocker = makeMsg({ id: 'b1', message_type: 'BLOCKER', from_type: 'agent', content: 'blocker msg' });
    const feedback = makeMsg({ id: 'f1', message_type: 'FEEDBACK', from_type: 'user', content: 'feedback msg' });
    const el = await renderPanel([blocker, feedback]);

    // Both should be visible initially
    expect(el.textContent).toContain('blocker msg');
    expect(el.textContent).toContain('feedback msg');

    // Toggle critical-only filter
    const filterBtn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Critical only'));
    await act(async () => { (filterBtn as HTMLButtonElement).click(); });

    // Only blocker should be visible
    expect(el.textContent).toContain('blocker msg');
    expect(el.textContent).not.toContain('feedback msg');
  });

  test('critical-only filter shows QUESTION when active', async () => {
    const bug = makeMsg({ id: 'b1', message_type: 'BUG_REPORT', from_type: 'agent', content: 'bug msg' });
    const question = makeMsg({ id: 'q1', message_type: 'QUESTION', from_type: 'agent', content: 'question msg' });
    const feedback = makeMsg({ id: 'f1', message_type: 'FEEDBACK', from_type: 'user', content: 'feedback msg' });
    const el = await renderPanel([bug, question, feedback]);

    // Toggle critical-only
    const filterBtn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Critical only'));
    await act(async () => { (filterBtn as HTMLButtonElement).click(); });

    expect(el.textContent).toContain('bug msg');
    expect(el.textContent).toContain('question msg');
    expect(el.textContent).not.toContain('feedback msg');
  });

  test('critical-only filter shows critical NOTEs (failure_alert)', async () => {
    const failureAlert = makeMsg({
      id: 'fa1',
      message_type: 'NOTE',
      from_type: 'system',
      context: { event_type: 'failure_alert' },
      content: 'failure alert msg',
    });
    const feedback = makeMsg({ id: 'f1', message_type: 'FEEDBACK', from_type: 'user', content: 'feedback msg' });
    const el = await renderPanel([failureAlert, feedback]);

    // Toggle critical-only
    const filterBtn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Critical only'));
    await act(async () => { (filterBtn as HTMLButtonElement).click(); });

    expect(el.textContent).toContain('failure alert msg');
    expect(el.textContent).not.toContain('feedback msg');
  });

  test('critical-only filter hides routine NOTEs (pr_merged)', async () => {
    const blocker = makeMsg({ id: 'b1', message_type: 'BLOCKER', from_type: 'agent', content: 'blocker msg' });
    const prMerged = makeMsg({
      id: 'pm1',
      message_type: 'NOTE',
      from_type: 'system',
      context: { event_type: 'pr_merged' },
      content: 'pr merged msg',
    });
    const el = await renderPanel([blocker, prMerged]);

    // Initially both visible
    expect(el.textContent).toContain('blocker msg');
    // prMerged is in routine ops (collapsed)
    expect(el.textContent).toContain('routine ops');

    // Toggle critical-only
    const filterBtn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Critical only'));
    await act(async () => { (filterBtn as HTMLButtonElement).click(); });

    expect(el.textContent).toContain('blocker msg');
    // Routine ops section should be gone
    expect(el.textContent).not.toContain('routine ops');
  });

  test('critical-only filter persists across reloads', async () => {
    const blocker = makeMsg({ id: 'b1', message_type: 'BLOCKER', from_type: 'agent' });
    const feedback = makeMsg({ id: 'f1', message_type: 'FEEDBACK', from_type: 'user', content: 'feedback' });
    const el = await renderPanel([blocker, feedback]);

    // Activate critical-only
    const filterBtn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Critical only'));
    await act(async () => { (filterBtn as HTMLButtonElement).click(); });

    // Verify saved in localStorage
    const prefs = JSON.parse(localStorage.getItem(key) || '{}');
    expect(prefs.criticalOnly).toBe(true);

    // Unmount and remount
    await act(async () => { root!.unmount(); });
    root = null;
    host?.remove();
    host = null;
    _mockMessagesFetch([blocker, feedback]);

    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root!.render(
        <MessagesPanel
          selectedProjectId="proj-1"
          onClose={() => {}}
          addToast={() => {}}
          onMessageCountChange={() => {}}
        />,
      );
    });
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });

    // Critical-only should still be active
    expect(host!.textContent).toContain('Blocker');
    expect(host!.textContent).not.toContain('feedback');
  });

  test('critical-only filter works with search', async () => {
    const blocker = makeMsg({ id: 'b1', message_type: 'BLOCKER', from_type: 'agent', content: 'system down' });
    const feedback = makeMsg({ id: 'f1', message_type: 'FEEDBACK', from_type: 'user', content: 'system down too' });
    const el = await renderPanel([blocker, feedback]);

    // Activate critical-only
    const filterBtn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Critical only'));
    await act(async () => { (filterBtn as HTMLButtonElement).click(); });

    // Search for "system down"
    const searchInput = el.querySelector('input[placeholder*="Search"]') as HTMLInputElement;
    await act(async () => {
      searchInput.value = 'system down';
      searchInput.dispatchEvent(new Event('change', { bubbles: true }));
    });

    // Only blocker matches (feedback is filtered by critical-only)
    const text = el.textContent || '';
    expect(text).toContain('system down');
    const matches = [...el.querySelectorAll('[data-testid="message-bubble"]')];
    expect(matches.length).toBeGreaterThanOrEqual(1);
  });

  test('button appearance changes when critical-only is active', async () => {
    const blocker = makeMsg({ id: 'b1', message_type: 'BLOCKER', from_type: 'agent' });
    const el = await renderPanel([blocker]);

    const filterBtn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Critical only')) as HTMLButtonElement;
    expect(filterBtn).toBeTruthy();

    // Initially not active (neutral colors)
    expect(filterBtn.className).toContain('text-neutral');

    // Click to activate
    await act(async () => { filterBtn.click(); });

    // Should now have amber/alert colors
    expect(filterBtn.className).toContain('text-amber');
    expect(filterBtn.className).toContain('amber-500');
  });
});

describe('MessagesPanel since-last-visit divider', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    // Set up a last-seen timestamp so the divider will render
    const dividerKey = 'messages_panel_last_seen:test-user@example.com:proj-1';
    localStorage.setItem(dividerKey, String(Date.now() - 60000)); // 1 minute ago
  });

  afterEach(() => {
    const r = root;
    if (r) act(() => r.unmount());
    root = null;
    host?.remove();
    host = null;
    localStorage.clear();
    vi.clearAllMocks();
    vi.useRealTimers();
  });

  async function renderPanel(messages: ReturnType<typeof makeMsg>[]) {
    // URL-keyed: the panel fires GET last-seen on mount BEFORE the messages
    // fetch, so a one-shot mock would feed the messages to the wrong call and
    // leave the list empty. Return null last-seen (tests seed localStorage) and
    // serve the messages payload for every other endpoint.
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
      const urlStr = url as string;
      const initObj = init as RequestInit | undefined;
      if (urlStr.includes('/api/admin/messages/last-seen') && initObj?.method !== 'PUT') {
        return new Response(JSON.stringify({ last_seen_at: null }), { status: 200 });
      }
      return new Response(JSON.stringify({ messages }), { status: 200 });
    });
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

  test('divider renders when there are only routine new messages (no high-signal)', async () => {
    const routineMsg = makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' } });
    const el = await renderPanel([routineMsg]);
    const divider = el.querySelector('[data-divider-marker]');
    expect(divider).toBeTruthy();
    expect(el.textContent).toContain('NEW since');
  });

  test('divider renders when there are both high-signal and routine new messages', async () => {
    const blocker = makeMsg({ id: 'b1', message_type: 'BLOCKER', from_type: 'agent', content: 'blocker' });
    const routine = makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' } });
    const el = await renderPanel([blocker, routine]);
    const divider = el.querySelector('[data-divider-marker]');
    expect(divider).toBeTruthy();
    expect(el.textContent).toContain('NEW since');
  });

  test('divider does not render when there are no new messages', async () => {
    // Create a last-seen timestamp very recent
    const recentKey = 'messages_panel_last_seen:test-user@example.com:proj-1';
    localStorage.setItem(recentKey, String(Date.now()));

    const routineMsg = makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' }, created_at: new Date(Date.now() - 120000).toISOString() });
    const el = await renderPanel([routineMsg]);
    const divider = el.querySelector('[data-divider-marker]');
    expect(divider).toBeFalsy();
  });

  test('scroll-to-dismiss works when divider is visible (routine-only new)', async () => {
    const routineMsg = makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' } });
    const el = await renderPanel([routineMsg]);
    const scrollContainer = el.querySelector('[class*="overflow-y-auto"]') as HTMLElement;
    expect(scrollContainer).toBeTruthy();

    // Divider should be present before scroll
    let divider = el.querySelector('[data-divider-marker]') as HTMLElement;
    expect(divider).toBeTruthy();

    // The dismissal handler is synchronous (no timers) and dismisses when the
    // divider's getBoundingClientRect().bottom < 0. jsdom returns all-zero rects
    // by default, so stub the rect to place the divider above the viewport.
    divider.getBoundingClientRect = () =>
      ({ bottom: -10, top: -20, left: 0, right: 0, width: 0, height: 10, x: 0, y: -20, toJSON: () => ({}) }) as DOMRect;

    await act(async () => {
      scrollContainer.dispatchEvent(new Event('scroll', { bubbles: true }));
    });

    // After dismissal, the divider should no longer appear
    divider = el.querySelector('[data-divider-marker]') as HTMLElement;
    expect(divider).toBeFalsy();
  });

  test('Mark all read button dismisses divider', async () => {
    const routineMsg = makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' } });
    const el = await renderPanel([routineMsg]);

    // Find the "All caught up" button on the divider
    const buttons = [...el.querySelectorAll('button')].filter(b => b.textContent?.includes('All caught up'));
    expect(buttons.length).toBeGreaterThan(0);

    const dividerButton = buttons[0] as HTMLButtonElement;
    await act(async () => { dividerButton.click(); });

    // Divider should be gone
    const divider = el.querySelector('[data-divider-marker]');
    expect(divider).toBeFalsy();
  });
});
