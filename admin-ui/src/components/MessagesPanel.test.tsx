import { describe, test, expect, vi, afterEach } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { render, waitFor } from '@testing-library/react';
import { MessagesPanel } from './MessagesPanel';
import { classifySignalLevel } from './MessagesPanel.utils';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response(JSON.stringify({ messages: [] }), { status: 200 })),
  API_BASE_URL: 'http://localhost:8000',
}));

import { apiFetch } from '../api';

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

  afterEach(() => {
    const r = root;
    if (r) act(() => r.unmount());
    root = null;
    host?.remove();
    host = null;
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
