import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, test } from 'vitest';
import type { StrategicStateTaskFocus, StrategicStateTaskFocusItem } from '../strategicStateApi';
import type { ResidentActivityState, StrategicChatLiveState } from '../utils/strategicSessionLive';
import type { ProjectMessage } from '../types/messages';
import {
  buildConversationWorkdayStatus,
  type ConversationWorkdayStatus,
} from '../utils/conversationWorkdayStatus';
import { ConversationUtilityBar } from './ConversationUtilityBar';

let host: HTMLDivElement | null = null;
let root: Root | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  host?.remove();
  host = null;
});

function task(
  id: string,
  bucket: 'needs_judgment' | 'friction' | 'active',
  status: string,
  title: string,
  overrides: Partial<StrategicStateTaskFocusItem> = {},
): StrategicStateTaskFocusItem {
  return {
    id,
    bucket,
    status,
    title,
    requires_approval: false,
    reasons: [],
    ...overrides,
  };
}

function focus(
  needsJudgment: StrategicStateTaskFocusItem[] = [],
  friction: StrategicStateTaskFocusItem[] = [],
  active: StrategicStateTaskFocusItem[] = [],
): StrategicStateTaskFocus {
  const bucket = (items: StrategicStateTaskFocusItem[]) => ({
    total: items.length,
    limit: 8,
    has_more: false,
    items,
  });
  return {
    version: 1,
    bucket_limit: 8,
    has_more: false,
    buckets: {
      needs_judgment: bucket(needsJudgment),
      friction: bucket(friction),
      active: bucket(active),
    },
  };
}

function activity(overrides: Partial<ResidentActivityState> = {}): ResidentActivityState {
  return {
    phase: 'idle',
    durablePhase: 'waiting',
    label: 'Waiting',
    detail: 'Watching for project movement',
    ...overrides,
  };
}

function strategicMessage(
  id: string,
  context: Record<string, unknown>,
): ProjectMessage {
  return {
    id,
    project_id: 'project-1',
    message_type: 'NOTE',
    from_type: 'strategic',
    from_task_id: 'resident-task',
    content: 'Strategic reply',
    context,
    status: 'handled',
    resolution: null,
    resolved_at: null,
    created_at: '2026-08-15T00:00:00Z',
    sequence: 1,
    surface: 'conversation',
  } as ProjectMessage;
}

function renderBar(
  workdayStatus: ConversationWorkdayStatus,
  chatLiveState?: StrategicChatLiveState,
  interactionMode?: string,
) {
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => {
    root?.render(
      <ConversationUtilityBar
        expanded={false}
        onExpand={() => {}}
        onCollapse={() => {}}
        searchQuery=""
        onSearchChange={() => {}}
        workdayStatus={workdayStatus}
        chatLiveState={chatLiveState}
        interactionMode={interactionMode}
      />,
    );
  });
  return host;
}

describe('buildConversationWorkdayStatus', () => {
  test('shows Chat as Workday off without claiming other project autonomy stopped', () => {
    const status = buildConversationWorkdayStatus({
      interactionMode: 'chat',
      residentActivity: null,
      taskFocus: focus([], [
        task('d34da434-ec40-497c-8280-5f45a4a59612', 'friction', 'BLOCKED', 'Viewport capture', {
          blocked_reason: 'Chromium bootstrap unavailable',
        }),
      ]),
    });

    expect(status.state).toBe('off');
    expect(status.label).toBe('Workday off');
    expect(status.detail).toContain('d34da434 · blocked · Viewport capture');
    expect(status.title).toContain('already-dispatched pipeline work may continue');
    expect(status.title).toContain('other project autonomy may continue');
    expect(status.title).toContain('Chromium bootstrap unavailable');
  });

  test('shows Chat as Workday off after a normal stop retains an ended snapshot', () => {
    const status = buildConversationWorkdayStatus({
      interactionMode: 'chat',
      residentActivity: activity({
        phase: 'dead',
        durablePhase: 'ended',
        label: 'Session ended',
      }),
    });

    expect(status.state).toBe('off');
    expect(status.label).toBe('Workday off');
    expect(status.title).not.toContain('needs restart');
  });

  test('maps a live executing Resident to working and prioritizes friction over active work', () => {
    const status = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: activity({
        phase: 'executing',
        durablePhase: 'working',
        label: 'Working',
        detail: 'Workday is investigating a Fast consultation',
      }),
      residentTaskId: 'resident-task',
      taskFocus: focus(
        [],
        [task('blocked-task', 'friction', 'BLOCKED', 'Repair viewport gate')],
        [
          task('resident-task', 'active', 'IN_PROGRESS', 'Strategic review'),
          task('implementation-task', 'active', 'IN_PROGRESS', 'Implement design rules'),
        ],
      ),
    });

    expect(status.state).toBe('working');
    expect(status.label).toBe('Workday working');
    expect(status.detail).toContain('blocked- · blocked · Repair viewport gate');
    expect(status.detail).not.toContain('Strategic review');
  });

  test('shows requested, running, and delivering consultation states without parsing prose', () => {
    const request = strategicMessage('11111111-1111-4111-8111-111111111111', {
      message_role: 'operator_reply',
      _workday_consultation_requested: true,
    });
    const requested = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: activity(),
      conversationMessages: [request],
    });
    expect(requested.state).toBe('consultation_queued');
    expect(requested.label).toBe('Workday requested');

    const running = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: activity({
        phase: 'executing',
        durablePhase: 'working',
        reasonCode: 'consultation_running',
        consultationId: request.id,
        autonomyState: 'paused',
        autonomyReasonCode: 'consultation_running',
        detail: 'Unrelated human prose that must not be parsed',
      }),
      conversationMessages: [request],
    });
    expect(running.state).toBe('consultation_running');
    expect(running.label).toBe('Workday consulting');

    const delivering = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: activity({
        phase: 'executing',
        durablePhase: 'working',
        reasonCode: 'consultation_delivering',
        consultationId: request.id,
        autonomyState: 'paused',
        autonomyReasonCode: 'consultation_delivering',
      }),
      conversationMessages: [request],
    });
    expect(delivering.state).toBe('consultation_delivering');
    expect(delivering.label).toBe('Workday findings ready');
    expect(delivering.title).toContain('Fast is preparing the public continuation');
  });

  test('keeps a server-verified running consultation visible outside the message window', () => {
    const consultationId = '77777777-7777-4777-8777-777777777777';
    const runningActivity = activity({
      phase: 'executing',
      durablePhase: 'working',
      reasonCode: 'consultation_running',
      consultationId,
      consultationVerified: true,
      autonomyState: 'paused',
      autonomyReasonCode: 'consultation_running',
    });
    expect(buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: runningActivity,
      conversationMessages: [],
    }).state).toBe('consultation_running');

    const continuation = strategicMessage('continuation-window', {
      message_role: 'workday_consultation_continuation',
      _workday_result_packet_id: 'result-window',
      _workday_consultation_atomic_reply_id: consultationId,
    });
    expect(buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: runningActivity,
      conversationMessages: [continuation],
    }).state).toBe('waiting');
  });

  test('keeps Chat mode consultation visible and clears stale working after its continuation', () => {
    const consultationId = '22222222-2222-4222-8222-222222222222';
    const request = strategicMessage(consultationId, {
      message_role: 'operator_reply',
      _workday_consultation_requested: true,
    });
    const runningActivity = activity({
      phase: 'executing',
      durablePhase: 'working',
      reasonCode: 'consultation_running',
      consultationId,
      autonomyState: 'paused',
      autonomyReasonCode: 'consultation_running',
    });
    expect(buildConversationWorkdayStatus({
      interactionMode: 'chat',
      residentActivity: runningActivity,
      conversationMessages: [request],
    }).state).toBe('consultation_running');

    const continuation = strategicMessage('continuation-a', {
      message_role: 'workday_consultation_continuation',
      _workday_result_packet_id: 'result-a',
      _workday_consultation_atomic_reply_id: consultationId,
    });
    const settled = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: runningActivity,
      conversationMessages: [request, continuation],
    });
    expect(settled.state).toBe('waiting');
    expect(settled.label).toBe('Workday waiting');
  });

  test('does not clear a second queued consultation when the first continuation arrives', () => {
    const requestA = strategicMessage('33333333-3333-4333-8333-333333333333', {
      message_role: 'operator_reply',
      _workday_consultation_requested: true,
    });
    const requestB = strategicMessage('44444444-4444-4444-8444-444444444444', {
      message_role: 'operator_reply',
      _workday_consultation_requested: true,
    });
    const continuationA = strategicMessage('continuation-a', {
      message_role: 'workday_consultation_continuation',
      _workday_result_packet_id: 'result-a',
      _workday_consultation_atomic_reply_id: requestA.id,
    });
    const status = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: activity({
        phase: 'executing',
        durablePhase: 'working',
        reasonCode: 'consultation_running',
        consultationId: requestA.id,
        autonomyState: 'paused',
        autonomyReasonCode: 'consultation_running',
      }),
      conversationMessages: [requestA, requestB, continuationA],
    });
    expect(status.state).toBe('consultation_queued');
    expect(status.label).toBe('Workday requested');
  });

  test('keeps queued request count and structured pause reason visible without hover', () => {
    const requestA = strategicMessage('88888888-8888-4888-8888-888888888888', {
      message_role: 'operator_reply',
      _workday_consultation_requested: true,
    });
    const requestB = strategicMessage('99999999-9999-4999-8999-999999999999', {
      message_role: 'operator_reply',
      _workday_consultation_requested: true,
    });
    const status = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: activity({
        phase: 'idle',
        durablePhase: 'waiting',
        reasonCode: 'consultation_queued',
        consultationId: requestA.id,
        consultationVerified: true,
        autonomyState: 'paused',
        autonomyReasonCode: 'authority_not_held',
      }),
      conversationMessages: [requestA, requestB],
    });

    expect(status.state).toBe('consultation_queued');
    expect(status.label).toBe('Workday requested · paused');
    // The open-request count is secondary; the pause reason AND the
    // Resident's current activity ("Watching for project movement" from
    // the default activity() fixture) must both stay in the visible slot —
    // the count alone must never displace them.
    expect(status.detail).toBe('2 open requests · authority not held · Watching for project movement');
    expect(status.compactLabel).toBe('requested · paused');
    expect(status.compactDetail).toBe('2 open · no authority');
    const rendered = renderBar(status);
    const visibleLine = rendered.querySelector('[data-testid="conversation-workday-status"]');
    expect(visibleLine?.textContent).toContain('Workday requested · paused');
    expect(visibleLine?.textContent).toContain('2 open requests · authority not held');
  });

  test('preserves open request count when Workday is dead or unavailable', () => {
    const request = strategicMessage('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', {
      message_role: 'operator_reply',
      _workday_consultation_requested: true,
    });
    const dead = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: activity({
        phase: 'dead',
        durablePhase: 'ended',
        label: 'Needs restart',
      }),
      conversationMessages: [request],
    });
    expect(dead.state).toBe('needs_restart');
    expect(dead.label).toBe('Workday requested · needs restart');
    expect(dead.detail).toBe('1 open request · Watching for project movement');

    const unavailable = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: null,
      residentAvailability: {
        available: false,
        state: 'blocked',
        reason: 'No eligible runner',
      },
      conversationMessages: [request],
    });
    expect(unavailable.state).toBe('unavailable');
    expect(unavailable.label).toBe('Workday requested · unavailable');
    expect(unavailable.detail).toBe('1 open request');
    expect(unavailable.title).toContain('No eligible runner');
  });

  test('ignores spoofed receipts and mismatched structured consultation ids', () => {
    const canonicalRequest = strategicMessage('55555555-5555-4555-8555-555555555555', {
      message_role: 'operator_reply',
      _workday_consultation_requested: true,
    });
    const spoofed = {
      ...strategicMessage('spoofed-request', {
        message_role: 'operator_reply',
        _workday_consultation_requested: true,
      }),
      from_type: 'user',
    } as ProjectMessage;
    const status = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: activity({
        phase: 'executing',
        durablePhase: 'working',
        reasonCode: 'consultation_running',
        consultationId: '66666666-6666-4666-8666-666666666666',
        autonomyState: 'paused',
        autonomyReasonCode: 'consultation_running',
      }),
      conversationMessages: [spoofed, canonicalRequest],
    });
    expect(status.state).toBe('consultation_queued');
    expect(status.label).toBe('Workday requested');
  });

  test('autonomy pause outranks a stale generic executing phase', () => {
    const foreground = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: activity({
        phase: 'executing',
        durablePhase: 'working',
        autonomyState: 'paused',
        autonomyReasonCode: 'foreground_call',
      }),
    });
    expect(foreground.state).toBe('paused');
    expect(foreground.label).toBe('Workday on call');

    const lapsed = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: activity({
        phase: 'executing',
        durablePhase: 'working',
        autonomyState: 'paused',
        autonomyReasonCode: 'authority_not_held',
      }),
    });
    expect(lapsed.state).toBe('paused');
    expect(lapsed.label).toBe('Workday paused');
  });

  test('prioritizes operator judgment over friction and active work', () => {
    const status = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: activity(),
      taskFocus: focus(
        [task('approval-task', 'needs_judgment', 'AWAITING_APPROVAL', 'Approve production action')],
        [task('blocked-task', 'friction', 'BLOCKED', 'Repair viewport gate')],
        [task('active-task', 'active', 'IN_PROGRESS', 'Implement design rules')],
      ),
    });

    expect(status.detail).toContain('approval · awaiting approval · Approve production action');
    expect(status.detail).not.toContain('Repair viewport gate');
  });

  test('maps durable retry and dead states without inventing activity', () => {
    expect(buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: activity({ phase: 'idle', durablePhase: 'retrying', label: 'Retrying' }),
    }).state).toBe('retrying');

    const stopped = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: activity({ phase: 'dead', durablePhase: 'ended', label: 'Workday needs restart' }),
    });
    expect(stopped.state).toBe('needs_restart');
    expect(stopped.label).toBe('Workday needs restart');
    expect(stopped.title).toContain('other project autonomy may continue');

    const stopping = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: activity({
        phase: 'idle',
        durablePhase: 'stopping',
        label: 'Stopping soon',
      }),
    });
    expect(stopping.state).toBe('stopping');
    expect(stopping.label).toBe('Workday stopping');
  });

  test('uses server availability when a Continuous session has no activity snapshot yet', () => {
    const starting = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: null,
      residentAvailability: { available: true, state: 'starting', reason: 'Runner claim pending' },
    });
    expect(starting.state).toBe('starting');
    expect(starting.label).toBe('Workday starting');

    const ready = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: null,
      residentAvailability: { available: true, state: 'ready', reason: null },
    });
    expect(ready.state).toBe('ready');
    expect(ready.label).toBe('Workday ready');
    expect(ready.title).toContain('no active Resident session');

    const unavailable = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: null,
      residentAvailability: { available: false, state: 'blocked', reason: 'No eligible runner' },
    });
    expect(unavailable.state).toBe('unavailable');
    expect(unavailable.label).toBe('Workday unavailable');
  });

  test('surfaces the live activity and task detail in the visible slot during an open consultation, not only the title', () => {
    const request = strategicMessage('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', {
      message_role: 'operator_reply',
      _workday_consultation_requested: true,
    });
    const status = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: activity({
        detail: 'Investigating the flaky vitest suite',
      }),
      taskFocus: focus([], [
        task('friction-task-9999', 'friction', 'BLOCKED', 'Repair viewport gate'),
      ]),
      conversationMessages: [request],
    });

    expect(status.state).toBe('consultation_queued');
    // Both the live activity narration and the queued-task focus item must be
    // reachable in the visible `detail` — not only joined into the hover-only
    // `title`, which is unusable on a phone.
    expect(status.detail).toContain('Investigating the flaky vitest suite');
    expect(status.title).toContain('Investigating the flaky vitest suite');
  });

  test('exposes when the Resident last acted so a live and a wedged session do not render identically', () => {
    const idleAtMs = Date.parse('2026-08-15T00:00:00Z');
    const idle = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: activity({ phase: 'idle', durablePhase: 'waiting', endMs: idleAtMs }),
    });
    expect(idle.lastActivityMs).toBe(idleAtMs);

    const executingAtMs = Date.parse('2026-08-15T00:05:00Z');
    const executing = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: activity({
        phase: 'executing',
        durablePhase: 'working',
        startMs: executingAtMs,
      }),
    });
    expect(executing.lastActivityMs).toBe(executingAtMs);

    const noSession = buildConversationWorkdayStatus({
      interactionMode: 'chat',
      residentActivity: null,
    });
    expect(noSession.lastActivityMs).toBeNull();
  });
});

describe('ConversationUtilityBar detail toggle', () => {
  afterEach(() => {
    if (root) act(() => root?.unmount());
    root = null;
    host?.remove();
    host = null;
  });

  test('is absent without onToggleDetail and calls the handler with the flipped value when present', () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => {
      root?.render(
        <ConversationUtilityBar
          expanded={false}
          onExpand={() => {}}
          onCollapse={() => {}}
          searchQuery=""
          onSearchChange={() => {}}
        />,
      );
    });
    expect(host.querySelector('[data-testid="conversation-detail-toggle"]')).toBeNull();

    const calls: boolean[] = [];
    const onToggleDetail = (value: boolean) => { calls.push(value); };
    act(() => {
      root?.render(
        <ConversationUtilityBar
          expanded={false}
          onExpand={() => {}}
          onCollapse={() => {}}
          searchQuery=""
          onSearchChange={() => {}}
          detailRevealed={false}
          onToggleDetail={onToggleDetail}
        />,
      );
    });
    const toggle = host.querySelector('[data-testid="conversation-detail-toggle"]') as HTMLButtonElement | null;
    expect(toggle).not.toBeNull();
    expect(toggle?.getAttribute('aria-pressed')).toBe('false');
    act(() => { toggle?.click(); });
    expect(calls).toEqual([true]);
  });
});

describe('ConversationUtilityBar Workday status', () => {
  test('renders one bounded, server-derived status line with full detail in its title', () => {
    const status = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: activity(),
      taskFocus: focus([], [], [task(
        'implementation-task-1234',
        'active',
        'IN_PROGRESS',
        'A deliberately long implementation task title that remains bounded in the utility bar',
      )]),
    });
    const rendered = renderBar(status);
    const line = rendered.querySelector('[data-testid="conversation-workday-status"]');

    expect(line?.getAttribute('data-workday-state')).toBe('waiting');
    expect(line?.textContent).toContain('Workday waiting');
    expect(line?.textContent).toContain('implement');
    expect(line?.getAttribute('title')).toContain('A deliberately long implementation task title');
    expect(line?.getAttribute('role')).toBe('status');
    expect(line?.getAttribute('aria-live')).toBe('polite');
  });

  test('renders how long ago the Resident last acted without requiring a hover', () => {
    const fiveMinutesAgo = Date.now() - 5 * 60 * 1000;
    const status = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: activity({ phase: 'idle', durablePhase: 'waiting', endMs: fiveMinutesAgo }),
    });
    const rendered = renderBar(status);
    const age = rendered.querySelector('[data-testid="conversation-workday-age"]');

    expect(age?.textContent).toBe('5m ago');
  });

  test('renders the live Chat turn in the same awareness slot instead of Workday off', () => {
    const workdayStatus = buildConversationWorkdayStatus({ interactionMode: 'chat', residentActivity: null });
    const rendered = renderBar(workdayStatus, {
      phase: 'streaming',
      label: 'Replying',
      detail: 'grep release gate',
    });
    const line = rendered.querySelector('[data-testid="conversation-workday-status"]');

    expect(line).toHaveTextContent('Replying');
    expect(line).toHaveTextContent('grep release gate');
    expect(line).not.toHaveTextContent('Workday off');
    expect(line).toHaveAttribute('data-chat-live-phase', 'streaming');
    expect(line).not.toHaveAttribute('data-workday-state');
    expect(line).toHaveAttribute('aria-label', 'Strategic reply status');
    expect(line?.className).not.toContain('hidden');
  });

  test('the Workday readout still owns the slot in continuous mode, even while a chat live state is non-null', () => {
    // `computeStrategicChatLiveState` is non-null for exactly the lifecycles
    // the Workday Resident runs under ('working'/'responding'), and App renders
    // ONE MessagesPanel for both modes. Without the mode gate the operator sees
    // a generic 'Working' chip instead of the Workday readout — losing
    // data-workday-state, the compact/full label, the detail and the age stamp.
    const fiveMinutesAgo = Date.now() - 5 * 60 * 1000;
    const workdayStatus = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: activity({
        phase: 'executing',
        durablePhase: 'working',
        label: 'Working',
        detail: 'Investigating the flaky vitest suite',
        startMs: fiveMinutesAgo,
      }),
    });
    const rendered = renderBar(
      workdayStatus,
      { phase: 'streaming', label: 'Replying', detail: 'grep release gate' },
      'continuous',
    );
    const line = rendered.querySelector('[data-testid="conversation-workday-status"]');

    expect(line).toHaveAttribute('data-workday-state', 'working');
    expect(line).not.toHaveAttribute('data-chat-live-phase');
    expect(line).not.toHaveAttribute('aria-label', 'Strategic reply status');
    expect(line).toHaveTextContent('Workday working');
    expect(line).toHaveTextContent('Investigating the flaky vitest suite');
    expect(line).not.toHaveTextContent('Replying');
    expect(line).not.toHaveTextContent('grep release gate');
    expect(rendered.querySelector('[data-testid="conversation-workday-age"]')?.textContent).toBe('5m ago');
  });

  test('the chat affordance still owns the slot in chat mode when the mode is passed explicitly', () => {
    const workdayStatus = buildConversationWorkdayStatus({ interactionMode: 'chat', residentActivity: null });
    const rendered = renderBar(
      workdayStatus,
      { phase: 'streaming', label: 'Replying', detail: 'grep release gate' },
      'chat',
    );
    const line = rendered.querySelector('[data-testid="conversation-workday-status"]');

    expect(line).toHaveAttribute('data-chat-live-phase', 'streaming');
    expect(line).toHaveTextContent('Replying');
    expect(line).not.toHaveTextContent('Workday off');
  });
});
