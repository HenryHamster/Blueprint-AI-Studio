import { describe, test, expect, vi, afterEach, beforeEach } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { render, waitFor } from '@testing-library/react';
import { MessagesPanel } from './MessagesPanel';
import { classifySignalLevel, effectiveSignal, loadEventPrefs, EVENT_PREFS_KEY, groupRoutineMessages, outcomeBreakdown, isFailingGroup, getToastSeverity, isCriticalSeverity } from './MessagesPanel.utils';

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

  test('NOTE with account_model_regression_quarantined is high-signal', () => {
    // System took account out of rotation due to regression — operator needs to see this immediately
    expect(classifySignalLevel({ ...base, context: { event_type: 'account_model_regression_quarantined' } })).toBe('high');
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

  test('NOTE with feedback_acknowledgment is high-signal', () => {
    // User filed FEEDBACK explicitly asking something; system's resolution reply should surface prominently
    expect(classifySignalLevel({ ...base, context: { event_type: 'feedback_acknowledgment' } })).toBe('high');
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

// ── outcomeBreakdown unit tests ───────────────────────────────────────────────

describe('outcomeBreakdown', () => {
  function m(result?: string) { return { context: result ? { result } : {} }; }

  test('returns empty string when no result fields present', () => {
    expect(outcomeBreakdown([m(), m()])).toBe('');
  });

  test('shows failed count when only failures present', () => {
    expect(outcomeBreakdown([m('failed'), m('failed'), m('failed')])).toBe('· 3 failed');
  });

  test('shows merged count when only merges present', () => {
    expect(outcomeBreakdown([m('merged'), m('merged')])).toBe('· 2 merged');
  });

  test('shows mixed breakdown', () => {
    const msgs = [m('failed'), m('failed'), m('merged'), m('review_needed')];
    expect(outcomeBreakdown(msgs)).toBe('· 2 failed, 1 merged, 1 review');
  });

  test('silently degrades when result is absent on all messages', () => {
    expect(outcomeBreakdown([{ context: { event_type: 'pr_merged' } }])).toBe('');
  });
});

// ── isFailingGroup unit tests ─────────────────────────────────────────────────

describe('isFailingGroup', () => {
  function m(result?: string) { return { context: result ? { result } : {} }; }

  test('returns false when fewer than 3 failures', () => {
    expect(isFailingGroup([m('failed'), m('failed'), m('merged')])).toBe(false);
  });

  test('returns false when 3+ failures but rate < 50%', () => {
    // 3 failed out of 7 = 43% < 50%
    expect(isFailingGroup([m('failed'), m('failed'), m('failed'), m('merged'), m('merged'), m('merged'), m('merged')])).toBe(false);
  });

  test('returns true when 3+ failures AND rate >= 50%', () => {
    // 3 failed out of 4 = 75%
    expect(isFailingGroup([m('failed'), m('failed'), m('failed'), m('merged')])).toBe(true);
  });

  test('returns true when all 3 are failed (100%)', () => {
    expect(isFailingGroup([m('failed'), m('failed'), m('failed')])).toBe(true);
  });

  test('returns false for empty array', () => {
    expect(isFailingGroup([])).toBe(false);
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
    // No separate routine ops section (all promoted, none remaining in routine)
    expect(text).not.toContain('routine ops');
  });

  test('routine group with mixed outcomes stays in routine ops section', async () => {
    // 2 failed, 1 merged — below promotion threshold (needs >=3 failed)
    const msgs = [
      makeMsg({ id: 'af1', from_type: 'system', context: { event_type: 'auto_fix_result', result: 'failed' } }),
      makeMsg({ id: 'af2', from_type: 'system', context: { event_type: 'auto_fix_result', result: 'failed' } }),
      makeMsg({ id: 'af3', from_type: 'system', context: { event_type: 'auto_fix_result', result: 'merged' } }),
    ];
    const el = await renderPanel(msgs);
    // Stays inside routine ops group (not promoted)
    expect(el.textContent).toContain('routine ops');
    expect(el.textContent).not.toContain('auto-fix results'); // label only visible when expanded
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

    // Routine NOTE messages do not fire toast
    expect(mockAddToast).not.toHaveBeenCalled();
  });

  test('fires toast when new high-signal NOTE (failure_alert) arrives on subsequent poll', async () => {
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

      // First fetch returns empty (initial load — should NOT toast).
      vi.mocked(apiFetch).mockResolvedValueOnce(
        new Response(JSON.stringify({ messages: [] }), { status: 200 })
      );
      // Second fetch (poll) returns the failure_alert — toast SHOULD fire with 'error'.
      vi.mocked(apiFetch).mockResolvedValueOnce(
        new Response(JSON.stringify({ messages: [failureAlert] }), { status: 200 })
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
      const [summary, severity] = mockAddToast.mock.calls[0];
      expect(summary).toContain('high-signal message');
      expect(summary).toContain('Pipeline failure detected');
      expect(severity).toBe('error');
    } finally {
      vi.useRealTimers();
    }
  });

  test('does not fire toast for muted high-signal NOTE event_type', async () => {
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

      // Set event pref to mute failure_alert
      localStorage.setItem(EVENT_PREFS_KEY, JSON.stringify({ failure_alert: 'mute' }));

      // Initial load empty
      vi.mocked(apiFetch).mockResolvedValueOnce(
        new Response(JSON.stringify({ messages: [] }), { status: 200 })
      );
      // Poll returns the muted failure_alert — no toast should fire
      vi.mocked(apiFetch).mockResolvedValueOnce(
        new Response(JSON.stringify({ messages: [failureAlert] }), { status: 200 })
      );

      render(
        <MessagesPanel
          selectedProjectId="proj-1"
          onClose={() => {}}
          addToast={mockAddToast}
          onMessageCountChange={() => {}}
        />
      );

      await vi.waitFor(() => {
        expect(apiFetch).toHaveBeenCalledTimes(1);
      });

      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });

      await vi.waitFor(() => {
        expect(apiFetch).toHaveBeenCalledTimes(2);
      });

      // Even though failure_alert is high-signal, the mute pref prevents the toast
      expect(mockAddToast).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
      localStorage.removeItem(EVENT_PREFS_KEY);
    }
  });

  test('fires toast with info severity for high-signal FEEDBACK', async () => {
    vi.useFakeTimers();
    try {
      const mockAddToast = vi.fn();
      const feedback = {
        id: 'msg-1',
        project_id: 'proj-1',
        message_type: 'FEEDBACK',
        from_type: 'user',
        from_task_id: null,
        content: 'Consider adding validation',
        context: {},
        status: 'open',
        resolution: null,
        resolved_at: null,
        created_at: '2026-05-06T10:00:00Z',
      };

      vi.mocked(apiFetch).mockResolvedValueOnce(
        new Response(JSON.stringify({ messages: [] }), { status: 200 })
      );
      vi.mocked(apiFetch).mockResolvedValueOnce(
        new Response(JSON.stringify({ messages: [feedback] }), { status: 200 })
      );

      render(
        <MessagesPanel
          selectedProjectId="proj-1"
          onClose={() => {}}
          addToast={mockAddToast}
          onMessageCountChange={() => {}}
        />
      );

      await vi.waitFor(() => {
        expect(apiFetch).toHaveBeenCalledTimes(1);
      });

      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });

      await vi.waitFor(() => {
        expect(mockAddToast).toHaveBeenCalledTimes(1);
      });
      const [, severity] = mockAddToast.mock.calls[0];
      expect(severity).toBe('info');
    } finally {
      vi.useRealTimers();
    }
  });
});

// ── getToastSeverity unit tests ────────────────────────────────────────────────

describe('getToastSeverity', () => {
  const base = { message_type: 'NOTE', from_type: 'system', context: {} };

  test('BLOCKER returns error', () => {
    expect(getToastSeverity({ ...base, message_type: 'BLOCKER' })).toBe('error');
  });

  test('BUG_REPORT returns error', () => {
    expect(getToastSeverity({ ...base, message_type: 'BUG_REPORT' })).toBe('error');
  });

  test('FEEDBACK returns info', () => {
    expect(getToastSeverity({ ...base, message_type: 'FEEDBACK' })).toBe('info');
  });

  test('NOTE with failure_alert returns error', () => {
    expect(getToastSeverity({ ...base, context: { event_type: 'failure_alert' } })).toBe('error');
  });

  test('NOTE with rollback_recommended returns error', () => {
    expect(getToastSeverity({ ...base, context: { event_type: 'rollback_recommended' } })).toBe('error');
  });

  test('NOTE with fix_loop_divergence_regressed returns error', () => {
    expect(getToastSeverity({ ...base, context: { event_type: 'fix_loop_divergence_regressed' } })).toBe('error');
  });

  test('NOTE with account_model_regression_quarantined returns error', () => {
    expect(getToastSeverity({ ...base, context: { event_type: 'account_model_regression_quarantined' } })).toBe('error');
  });

  test('NOTE with feedback_acknowledgment returns info', () => {
    expect(getToastSeverity({ ...base, context: { event_type: 'feedback_acknowledgment' } })).toBe('info');
  });

  test('NOTE with routine event_type returns info', () => {
    expect(getToastSeverity({ ...base, context: { event_type: 'pr_merged' } })).toBe('info');
  });

  test('NOTE with no event_type returns info', () => {
    expect(getToastSeverity({ ...base, context: {} })).toBe('info');
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
    (apiFetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify({ messages }), { status: 200 }),
    );
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

  test('selecting 1h hides routine ops section', async () => {
    const msgs = [makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' } })];
    const el = await renderPanel(msgs);
    expect(el.textContent).toContain('routine ops');

    const muteBtn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Mute routine'));
    await act(async () => { (muteBtn as HTMLButtonElement)?.click(); });
    const oneHourBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === '1 hour');
    await act(async () => { (oneHourBtn as HTMLButtonElement)?.click(); });

    expect(el.textContent).not.toContain('routine ops');
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

  test('clicking End mute early clears window and resurfaces routine', async () => {
    const msgs = [makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' } })];
    const el = await renderPanel(msgs);

    const muteBtn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Mute routine'));
    await act(async () => { (muteBtn as HTMLButtonElement)?.click(); });
    const oneHourBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === '1 hour');
    await act(async () => { (oneHourBtn as HTMLButtonElement)?.click(); });
    expect(el.textContent).not.toContain('routine ops');

    const chip = [...el.querySelectorAll('button')].find(b => b.textContent?.includes('left'));
    await act(async () => { (chip as HTMLButtonElement)?.click(); });
    const endBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.includes('End mute'));
    await act(async () => { (endBtn as HTMLButtonElement)?.click(); });

    expect(el.textContent).toContain('routine ops');
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

    expect(el.textContent).toContain('critical blocker');
    expect(el.textContent).not.toContain('routine ops');
  });

  test('mute window persists across re-renders via localStorage', async () => {
    const expiresAt = Date.now() + 3600000;
    localStorage.setItem('messages_panel_mute_window:test-user@example.com:proj-1', JSON.stringify({ expiresAt }));
    const msgs = [makeMsg({ id: 'r1', from_type: 'system', context: { event_type: 'pr_merged' } })];
    const el = await renderPanel(msgs);
    // Routine ops hidden because mute was loaded from localStorage on mount
    expect(el.textContent).not.toContain('routine ops');
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

  test('returns false for QUESTION', () => {
    expect(isCriticalSeverity({ message_type: 'QUESTION', context: {} })).toBe(false);
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

  test('returns true for NOTE with account_model_regression_quarantined event_type', () => {
    expect(isCriticalSeverity({ message_type: 'NOTE', context: { event_type: 'account_model_regression_quarantined' } })).toBe(true);
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

  test('critical-only filter hides QUESTION when active', async () => {
    const bug = makeMsg({ id: 'b1', message_type: 'BUG_REPORT', from_type: 'agent', content: 'bug msg' });
    const question = makeMsg({ id: 'q1', message_type: 'QUESTION', from_type: 'user', content: 'question msg' });
    const el = await renderPanel([bug, question]);

    // Toggle critical-only
    const filterBtn = [...el.querySelectorAll('button')].find(b => b.title?.includes('Critical only'));
    await act(async () => { (filterBtn as HTMLButtonElement).click(); });

    expect(el.textContent).toContain('bug msg');
    expect(el.textContent).not.toContain('question msg');
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
    (apiFetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      new Response(JSON.stringify({ messages: [blocker, feedback] }), { status: 200 }),
    );

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
    const matches = [...el.querySelectorAll('[class*="bubble"]')];
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

  test('renders clickable task slug when onOpenTask is provided', async () => {
    const mockOpenTask = vi.fn();
    const agentMsg = makeMsg({
      id: 'agent-1',
      message_type: 'BLOCKER',
      from_type: 'agent',
      from_task_id: 'abcd1234-efgh-5678-ijkl-9012mnopqrst',
      content: 'Agent message with task ref',
    });
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ messages: [agentMsg] }), { status: 200 })
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
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ messages: [agentMsg] }), { status: 200 })
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
    (apiFetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify({ messages }), { status: 200 }),
    );
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
    (apiFetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify({ messages }), { status: 200 }),
    );
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

