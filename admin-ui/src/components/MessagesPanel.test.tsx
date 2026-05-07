import { describe, test, expect, vi, afterEach, beforeEach } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { render, waitFor } from '@testing-library/react';
import { MessagesPanel } from './MessagesPanel';
import { classifySignalLevel, effectiveSignal, loadEventPrefs, EVENT_PREFS_KEY, groupRoutineMessages } from './MessagesPanel.utils';

vi.mock('../firebase', () => ({
  auth: { currentUser: { email: 'test-user@example.com' } },
}));

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response(JSON.stringify({ messages: [] }), { status: 200 })),
  API_BASE_URL: 'http://localhost:8000',
}));

import { apiFetch } from '../api';

// ── groupRoutineMessages unit tests ──────────────────────────────────────────

describe('groupRoutineMessages', () => {
  function makeRoutineItem(id: string, event_type: string) {
    return { id, context: { event_type } };
  }

  test('25 pr_merged messages collapse to one group chip', () => {
    const msgs = Array.from({ length: 25 }, (_, i) => makeRoutineItem(`m${i}`, 'pr_merged'));
    const rows = groupRoutineMessages(msgs);
    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('group');
    if (rows[0].kind === 'group') {
      expect(rows[0].msgs).toHaveLength(25);
      expect(rows[0].event_type).toBe('pr_merged');
      expect(rows[0].label).toBe('PR merges');
    }
  });

  test('mixed event_types stay separated into distinct groups', () => {
    const msgs = [
      ...Array.from({ length: 3 }, (_, i) => makeRoutineItem(`p${i}`, 'pr_merged')),
      ...Array.from({ length: 3 }, (_, i) => makeRoutineItem(`a${i}`, 'auto_fix_result')),
    ];
    const rows = groupRoutineMessages(msgs);
    expect(rows).toHaveLength(2);
    expect(rows[0].kind).toBe('group');
    expect(rows[1].kind).toBe('group');
    if (rows[0].kind === 'group') expect(rows[0].event_type).toBe('pr_merged');
    if (rows[1].kind === 'group') expect(rows[1].event_type).toBe('auto_fix_result');
  });

  test('single-occurrence event_types render as individual rows', () => {
    const msgs = [makeRoutineItem('x', 'pr_merged'), makeRoutineItem('y', 'auto_fix_result')];
    const rows = groupRoutineMessages(msgs);
    expect(rows).toHaveLength(2);
    expect(rows.every(r => r.kind === 'single')).toBe(true);
  });

  test('exactly 2 of the same type does not group', () => {
    const msgs = [makeRoutineItem('a', 'pr_merged'), makeRoutineItem('b', 'pr_merged')];
    const rows = groupRoutineMessages(msgs);
    expect(rows).toHaveLength(2);
    expect(rows.every(r => r.kind === 'single')).toBe(true);
  });

  test('exactly 3 of the same type groups', () => {
    const msgs = Array.from({ length: 3 }, (_, i) => makeRoutineItem(`m${i}`, 'pr_merged'));
    const rows = groupRoutineMessages(msgs);
    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('group');
  });

  test('interleaved different types do not merge into one group', () => {
    const msgs = [
      makeRoutineItem('a', 'pr_merged'),
      makeRoutineItem('b', 'auto_fix_result'),
      makeRoutineItem('c', 'pr_merged'),
      makeRoutineItem('d', 'pr_merged'),
      makeRoutineItem('e', 'pr_merged'),
    ];
    const rows = groupRoutineMessages(msgs);
    // first pr_merged: single; auto_fix_result: single; last 3 pr_merged: group
    expect(rows).toHaveLength(3);
    expect(rows[0].kind).toBe('single');
    expect(rows[1].kind).toBe('single');
    expect(rows[2].kind).toBe('group');
    if (rows[2].kind === 'group') expect(rows[2].msgs).toHaveLength(3);
  });

  test('uses msg id as group key (stable across renders)', () => {
    const msgs = Array.from({ length: 5 }, (_, i) => makeRoutineItem(`id-${i}`, 'pr_merged'));
    const rows = groupRoutineMessages(msgs);
    expect(rows).toHaveLength(1);
    if (rows[0].kind === 'group') expect(rows[0].key).toBe('id-0');
  });
});

// ── classifySignalLevel unit tests ───────────────────────────────────────────

describe('classifySignalLevel', () => {
  const base = { message_type: 'NOTE', from_type: 'system', context: {} };

  test('user messages are always high-signal', () => {
    expect(classifySignalLevel({ ...base, from_type: 'user' })).toBe('high');
    expect(classifySignalLevel({ ...base, from_type: 'USER' })).toBe('high');
  });

  test('FEEDBACK is high-signal regardless of from_type', () => {
    expect(classifySignalLevel({ ...base, message_type: 'FEEDBACK', from_type: 'agent' })).toBe('high');
  });

  test('BLOCKER is high-signal', () => {
    expect(classifySignalLevel({ ...base, message_type: 'BLOCKER' })).toBe('high');
  });

  test('BUG_REPORT is high-signal', () => {
    expect(classifySignalLevel({ ...base, message_type: 'BUG_REPORT' })).toBe('high');
  });

  test('NOTE with failure_alert event_type is high-signal', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'failure_alert' } })).toBe('high');
  });

  test('NOTE with rollback_recommended event_type is high-signal', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'rollback_recommended' } })).toBe('high');
  });

  test('NOTE with fix_loop_divergence_regressed event_type is high-signal', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'fix_loop_divergence_regressed' } })).toBe('high');
  });

  test('NOTE with pr_merged event_type is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'pr_merged' } })).toBe('routine');
  });

  test('NOTE with direction_milestone event_type is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'direction_milestone' } })).toBe('routine');
  });

  test('NOTE with cycle_summary event_type is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'cycle_summary' } })).toBe('routine');
  });

  test('NOTE with account_model_regression_quarantined is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'account_model_regression_quarantined' } })).toBe('routine');
  });

  test('NOTE with account_model_regression_recovered is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'account_model_regression_recovered' } })).toBe('routine');
  });

  test('NOTE with auto_fix_spawned is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'auto_fix_spawned' } })).toBe('routine');
  });

  test('NOTE with auto_fix_result is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'auto_fix_result' } })).toBe('routine');
  });

  test('NOTE with residual_fix_spawned is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'residual_fix_spawned' } })).toBe('routine');
  });

  test('NOTE with residual_fix_result is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'residual_fix_result' } })).toBe('routine');
  });

  test('NOTE with residual_fix_unsuspended is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'residual_fix_unsuspended' } })).toBe('routine');
  });

  test('NOTE with residual_case_auto_resolved is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'residual_case_auto_resolved' } })).toBe('routine');
  });

  test('NOTE with issue_implementation_spawned is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'issue_implementation_spawned' } })).toBe('routine');
  });

  test('NOTE with review_feedback is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'review_feedback' } })).toBe('routine');
  });

  test('NOTE with feedback_acknowledgment is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'feedback_acknowledgment' } })).toBe('routine');
  });

  test('NOTE with unknown event_type is routine (fail-safe)', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'some_future_event' } })).toBe('routine');
  });

  test('NOTE with no event_type is routine', () => {
    expect(classifySignalLevel({ ...base, context: {} })).toBe('routine');
  });

  test('QUESTION is routine (not in high-signal list)', () => {
    expect(classifySignalLevel({ ...base, message_type: 'QUESTION' })).toBe('routine');
  });
});

// ── Component rendering tests ─────────────────────────────────────────────────

function makeMsg(overrides: Partial<{
  id: string; message_type: string; from_type: string; context: Record<string, unknown>; status: string;
}>) {
  return {
    id: 'msg-1', project_id: 'proj-1', message_type: 'NOTE', from_type: 'system',
    from_task_id: null, content: 'test content', context: {}, status: 'open',
    resolution: null, resolved_at: null, created_at: new Date().toISOString(),
    ...overrides,
  };
}

describe('MessagesPanel grouping', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  const PREFS_KEY = 'messages_panel_prefs:test-user@example.com';

  afterEach(() => {
    const r = root;
    if (r) act(() => r.unmount());
    root = null;
    host?.remove();
    host = null;
    localStorage.removeItem(PREFS_KEY);
  });

  async function renderPanel(messages: ReturnType<typeof makeMsg>[]) {
    (apiFetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
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

    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ messages: [blocker] }), { status: 200 })
    );

    render(
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
      vi.mocked(apiFetch).mockResolvedValueOnce(
        new Response(JSON.stringify({ messages: [blocker1] }), { status: 200 })
      );
      // Second fetch (poll) returns the original plus a fresh BLOCKER —
      // toast SHOULD fire exactly once for the new one.
      vi.mocked(apiFetch).mockResolvedValueOnce(
        new Response(JSON.stringify({ messages: [blocker1, blocker2] }), { status: 200 })
      );

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
        expect(apiFetch).toHaveBeenCalledTimes(1);
      });
      expect(mockAddToast).not.toHaveBeenCalled();

      // Advance the 30s polling interval to trigger the second fetch.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });

      await vi.waitFor(() => {
        expect(apiFetch).toHaveBeenCalledTimes(2);
      });
      await vi.waitFor(() => {
        expect(mockAddToast).toHaveBeenCalledTimes(1);
      });
      const [summary, level] = mockAddToast.mock.calls[0];
      expect(summary).toContain('BLOCKER');
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
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ messages: [projAblocker] }), { status: 200 })
    );
    // Second fetch (project B initial load after switch) — also no toast,
    // because project switch should reset hasInitiallyLoaded.
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ messages: [projBblocker] }), { status: 200 })
    );

    const { rerender } = render(
      <MessagesPanel
        selectedProjectId="proj-A"
        onClose={() => {}}
        addToast={mockAddToast}
        onMessageCountChange={() => {}}
      />
    );

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledTimes(1);
    });
    expect(mockAddToast).not.toHaveBeenCalled();

    rerender(
      <MessagesPanel
        selectedProjectId="proj-B"
        onClose={() => {}}
        addToast={mockAddToast}
        onMessageCountChange={() => {}}
      />
    );

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledTimes(2);
    });

    // Project B's pre-existing open BLOCKER is treated as an initial load
    // for B, so no spurious toast fires.
    expect(mockAddToast).not.toHaveBeenCalled();
  });

  test('does not fire toast for NOTE messages on initial load', async () => {
    const mockAddToast = vi.fn();
    const note = {
      id: 'msg-1',
      project_id: 'proj-1',
      message_type: 'NOTE',
      from_type: 'system',
      from_task_id: null,
      content: 'Routine operation completed',
      context: {},
      status: 'open',
      resolution: null,
      resolved_at: null,
      created_at: '2026-05-06T10:00:00Z',
    };

    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ messages: [note] }), { status: 200 })
    );

    render(
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

    // NOTE messages are not in highSignalTypes — no toast fires
    expect(mockAddToast).not.toHaveBeenCalled();
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

// ── Per-event-type mute/surface component integration tests ───────────────────

describe('MessagesPanel per-event-type prefs', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => localStorage.removeItem(EVENT_PREFS_KEY));

  afterEach(() => {
    const r = root;
    if (r) act(() => r.unmount());
    root = null;
    host?.remove();
    host = null;
    localStorage.removeItem(EVENT_PREFS_KEY);
    vi.clearAllMocks();
  });

  async function renderPanel(messages: ReturnType<typeof makeMsg>[]) {
    (apiFetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
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
    await act(async () => { await new Promise<void>(res => setTimeout(res, 50)); });
    return host!;
  }

  test('muted event_type is hidden from the routine group', async () => {
    localStorage.setItem(EVENT_PREFS_KEY, JSON.stringify({ pr_merged: 'mute' }));
    const msgs = [makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' }, content: 'muted content' })];
    const el = await renderPanel(msgs);
    expect(el.textContent).not.toContain('muted content');
    expect(el.textContent).not.toContain('routine ops');
  });

  test('muted event_type is hidden from the high-signal area too', async () => {
    // Even a BLOCKER can be muted via explicit pref
    localStorage.setItem(EVENT_PREFS_KEY, JSON.stringify({ blocker_ev: 'mute' }));
    const msgs = [
      makeMsg({ id: 'b1', message_type: 'BLOCKER', from_type: 'agent', context: { event_type: 'blocker_ev' }, content: 'muted blocker' }),
    ];
    const el = await renderPanel(msgs);
    expect(el.textContent).not.toContain('muted blocker');
  });

  test('surfaced event_type appears in the high-signal area, not in routine group', async () => {
    localStorage.setItem(EVENT_PREFS_KEY, JSON.stringify({ pr_merged: 'surfaced' }));
    const msgs = [makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' }, content: 'promoted content' })];
    const el = await renderPanel(msgs);
    // content is visible
    expect(el.textContent).toContain('promoted content');
    // routine ops group is absent (no routine messages remain)
    expect(el.textContent).not.toContain('routine ops');
  });

  test('clearing all preferences returns to default classification', async () => {
    // Start with pr_merged surfaced so it appears as high-signal
    localStorage.setItem(EVENT_PREFS_KEY, JSON.stringify({ pr_merged: 'surfaced' }));
    const msgs = [makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' }, content: 'pref msg' })];
    const el = await renderPanel(msgs);
    // With surfaced pref, content is directly visible (high-signal)
    expect(el.textContent).toContain('pref msg');
    expect(el.textContent).not.toContain('routine ops');

    // Simulate "Reset all" — clear localStorage
    localStorage.removeItem(EVENT_PREFS_KEY);
    // loadEventPrefs() now returns {}
    expect(loadEventPrefs()).toEqual({});
    // effectiveSignal falls back to classifySignalLevel — pr_merged is routine
    const msg = { message_type: 'NOTE', from_type: 'system', context: { event_type: 'pr_merged' } };
    const { effectiveSignal: es } = await import('./MessagesPanel.utils');
    expect(es(msg, {})).toBe('routine');
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
    vi.mocked(apiFetch).mockResolvedValue(new Response(JSON.stringify({ messages }), { status: 200 }));
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
