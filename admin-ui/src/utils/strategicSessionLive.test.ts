import { describe, it, expect } from 'vitest';
import {
  computeStrategicStreamingTurn,
  computeStrategicSessionActivity,
  computeResidentActivityState,
  RESIDENT_HEARTBEAT_STALE_MS,
  countStrategicReplies,
  isStrategicReplyDelta,
  scopeStrategicTranscript,
  computeStrategicChatLiveState,
  CHAT_LIVE_STALE_MS,
} from './strategicSessionLive';
import type { StrategicLiveActivity } from './strategicSessionLive';
import type { StrategicSessionDelta } from '../hooks/useLogViewer';
import type { ProjectMessage } from '../types/messages';
import type {
  StrategicResidentSessionSnapshot,
  StrategicStateCycle,
  StrategicStateResponse,
  StrategicStateTask,
} from '../strategicStateApi';

function delta(p: Partial<StrategicSessionDelta> & { id: number }): StrategicSessionDelta {
  return { session_id: 'ss-1', text: '', ...p };
}

function strategicMsg(created_at: string): ProjectMessage {
  return {
    id: `m-${created_at}`, project_id: 'p', message_type: 'NOTE' as ProjectMessage['message_type'],
    from_type: 'strategic' as ProjectMessage['from_type'], from_task_id: null, content: 'x',
    context: {}, status: 'open' as ProjectMessage['status'], resolution: null, resolved_at: null, created_at,
  };
}

function durableStrategicState({
  cycle = {},
  task = {},
  activePhase = 'strategic_review',
}: {
  cycle?: Partial<StrategicStateCycle>;
  task?: Partial<StrategicStateTask> | null;
  activePhase?: string | null;
} = {}): StrategicStateResponse['strategic'] {
  return {
    active_cycle: {
      id: 'cycle-1',
      outcome: 'phase2_running',
      has_active_task: true,
      stale: false,
      phase1_task_id: 'task-1',
      phase2_task_id: 'task-2',
      phase1_started_at: '2026-06-01T09:00:00Z',
      phase1_completed_at: '2026-06-01T10:00:00Z',
      phase2_started_at: '2026-06-01T11:00:00Z',
      phase2_completed_at: null,
      directions_created: 0,
      directions_retired: 0,
      tasks_spawned: 1,
      vision_updated: false,
      problem_model_changed: false,
      running_update_changed: false,
      ...cycle,
    },
    active_task: task === null ? null : {
      id: 'task-2',
      status: 'IN_PROGRESS',
      title: 'Review the strategic direction',
      task_type: 'strategic_review',
      latest_status_message: null,
      started_at: '2026-06-01T11:05:00Z',
      created_at: '2026-06-01T11:00:00Z',
      updated_at: '2026-06-01T11:30:00Z',
      ...task,
    },
    active_phase: activePhase,
    latest_cycle: null,
    refresh: { pending: null },
  };
}

describe('isStrategicReplyDelta', () => {
  it('treats message / reasoning / kind-less as reply, activity kinds as not', () => {
    expect(isStrategicReplyDelta('message')).toBe(true);
    expect(isStrategicReplyDelta('reasoning')).toBe(true);
    expect(isStrategicReplyDelta(undefined)).toBe(true);
    expect(isStrategicReplyDelta('command')).toBe(false);
    expect(isStrategicReplyDelta('tool_result')).toBe(false);
    expect(isStrategicReplyDelta('status')).toBe(false);
  });
});

describe('computeStrategicStreamingTurn', () => {
  it('builds the bubble from reply-kind deltas only (activity noise excluded)', () => {
    const transcript = [
      delta({ id: 1, seq: 1, kind: 'command', text: '$ ls' }),
      delta({ id: 2, seq: 2, kind: 'message', text: 'The plan ' }),
      delta({ id: 3, seq: 3, kind: 'tool_result', text: '✓ done' }),
      delta({ id: 4, seq: 4, kind: 'message', text: 'is cheaper.' }),
    ];
    const turn = computeStrategicStreamingTurn(transcript, []);
    // Contiguous reply fragments concatenate WITHOUT an injected separator — the
    // agent-side ~100ms flush split vanishes; activity frames stay excluded.
    expect(turn?.text).toBe('The plan is cheaper.');
    expect(turn?.text).not.toContain('$ ls');
    expect(turn?.text).not.toContain('done');
    expect(turn?.segments).toEqual([{ kind: 'reply', text: 'The plan is cheaper.' }]);
  });

  it('concatenates multi-fragment reply text but preserves each fragment\'s own newlines', () => {
    // One continuous reply run split across flush boundaries: the fragments carry
    // real paragraph breaks, a bullet list, and a fenced code block. The split
    // boundaries must NOT inject newlines, but every real newline must survive.
    const transcript = [
      delta({ id: 1, seq: 1, kind: 'message', text: 'First paragraph that ' }),
      delta({ id: 2, seq: 2, kind: 'message', text: 'keeps going.\n\n- bullet one\n- bullet' }),
      delta({ id: 3, seq: 3, kind: 'message', text: ' two\n\n```\ncode();\n```' }),
    ];
    const turn = computeStrategicStreamingTurn(transcript, []);
    expect(turn?.text).toBe(
      'First paragraph that keeps going.\n\n- bullet one\n- bullet two\n\n```\ncode();\n```',
    );
  });

  it('separates reasoning deltas from reply deltas into ordered segments', () => {
    const transcript = [
      delta({ id: 1, seq: 1, kind: 'reasoning', text: 'Let me think ' }),
      delta({ id: 2, seq: 2, kind: 'reasoning', text: 'about this.' }),
      delta({ id: 3, seq: 3, kind: 'message', text: 'Here is ' }),
      delta({ id: 4, seq: 4, kind: 'message', text: 'the answer.' }),
    ];
    const turn = computeStrategicStreamingTurn(transcript, []);
    // Contiguous same-kind fragments merge without separators; the two kinds split.
    expect(turn?.segments).toEqual([
      { kind: 'reasoning', text: 'Let me think about this.' },
      { kind: 'reply', text: 'Here is the answer.' },
    ]);
    // `text` is reply-only — reasoning never leaks into the primary reply content.
    expect(turn?.text).toBe('Here is the answer.');
  });

  it('shows a reasoning-only turn (thinking before any reply) as a reasoning segment', () => {
    const transcript = [
      delta({ id: 1, seq: 1, kind: 'reasoning', text: 'Considering options…' }),
    ];
    const turn = computeStrategicStreamingTurn(transcript, []);
    expect(turn?.segments).toEqual([{ kind: 'reasoning', text: 'Considering options…' }]);
    expect(turn?.text).toBe('');
  });

  it('returns null when only activity (no reply) has streamed', () => {
    const transcript = [
      delta({ id: 1, seq: 1, kind: 'command', text: '$ npm test' }),
      delta({ id: 2, seq: 2, kind: 'status', text: 'responding', status: 'responding' }),
    ];
    expect(computeStrategicStreamingTurn(transcript, [])).toBeNull();
  });

  it('filters deltas already superseded by a durable strategic reply (watermark)', () => {
    const transcript = [
      delta({ id: 1, seq: 1, kind: 'message', text: 'old reply', ts: '2026-06-01T10:00:00Z' }),
      delta({ id: 2, seq: 2, kind: 'message', text: 'new reply', ts: '2026-06-01T10:00:10Z' }),
    ];
    const turn = computeStrategicStreamingTurn(transcript, [strategicMsg('2026-06-01T10:00:05Z')]);
    expect(turn?.text).toBe('new reply');
  });

  it('previews in-flight reply text while a turn is streaming (no done frame yet)', () => {
    const transcript = [
      delta({ id: 1, seq: 1, kind: 'status', text: 'responding', status: 'responding' }),
      delta({ id: 2, seq: 2, kind: 'message', text: 'Here is my answer' }),
    ];
    // No durable reply has landed and the turn has not ended — the preview shows.
    const turn = computeStrategicStreamingTurn(transcript, []);
    expect(turn?.text).toBe('Here is my answer');
  });

  it('drops a completed turn\'s reply (incl. post-send narration) once its done frame lands', () => {
    // Reproduces the operator complaint: the strategist sends its durable reply,
    // then narrates a "Sent — here is a summary…" in the live stream. That post-send
    // narration carries a ts NEWER than the durable message's created_at, so the
    // time-only water mark alone would leave it as a SECOND bubble. The turn's `done`
    // frame (always last) supersedes it, so the streaming preview must be empty and
    // only the single durable persisted reply remains — exactly one chat entry.
    const transcript = [
      delta({ id: 1, seq: 1, kind: 'message', text: 'The plan is cheaper.', ts: '2026-06-01T10:00:00Z' }),
      delta({ id: 2, seq: 2, kind: 'message', text: 'Sent — here is a summary of what I said.', ts: '2026-06-01T10:00:08Z' }),
      delta({ id: 3, seq: 3, kind: 'status', text: 'done', status: 'done', ts: '2026-06-01T10:00:09Z' }),
    ];
    const durableReply = strategicMsg('2026-06-01T10:00:05Z');
    expect(computeStrategicStreamingTurn(transcript, [durableReply])).toBeNull();
  });

  it('keeps the NEXT turn\'s streaming reply after a prior turn\'s done frame', () => {
    const transcript = [
      delta({ id: 1, seq: 1, kind: 'message', text: 'first turn reply' }),
      delta({ id: 2, seq: 2, kind: 'status', text: 'done', status: 'done' }),
      delta({ id: 3, seq: 3, kind: 'message', text: 'second turn reply' }),
    ];
    const turn = computeStrategicStreamingTurn(transcript, []);
    expect(turn?.text).toBe('second turn reply');
  });
});

describe('computeStrategicSessionActivity', () => {
  it('collects activity one-liners and the latest lifecycle marker', () => {
    const transcript = [
      delta({ id: 1, seq: 1, kind: 'status', text: 'responding', status: 'responding' }),
      delta({ id: 2, seq: 2, kind: 'command', text: '$ ls' }),
      delta({ id: 3, seq: 3, kind: 'message', text: 'thinking' }),
      delta({ id: 4, seq: 4, kind: 'file', text: 'edited a.ts' }),
    ];
    const activity = computeStrategicSessionActivity(transcript, []);
    expect(activity.latestLifecycle).toBe('responding');
    expect(activity.items.map(i => i.text)).toEqual(['$ ls', 'edited a.ts']);
  });

  it('recognises working lifecycle for an autonomous turn', () => {
    const transcript = [
      delta({ id: 1, seq: 1, kind: 'status', text: 'working', status: 'working' }),
      delta({ id: 2, seq: 2, kind: 'command', text: '$ git log' }),
    ];
    const activity = computeStrategicSessionActivity(transcript, []);
    expect(activity.latestLifecycle).toBe('working');
    expect(activity.items.map(i => i.text)).toEqual(['$ git log']);
  });

  it('done after working clears the strip and sets latestLifecycle to done', () => {
    const transcript = [
      delta({ id: 1, seq: 1, kind: 'status', text: 'working', status: 'working' }),
      delta({ id: 2, seq: 2, kind: 'command', text: '$ old cmd' }),
      delta({ id: 3, seq: 3, kind: 'status', text: 'done', status: 'done' }),
    ];
    const activity = computeStrategicSessionActivity(transcript, []);
    expect(activity.latestLifecycle).toBe('done');
    expect(activity.items).toHaveLength(0);
  });

  it('drops activity up to and including the last done frame (completed turn)', () => {
    const transcript = [
      delta({ id: 1, seq: 1, kind: 'command', text: '$ old' }),
      delta({ id: 2, seq: 2, kind: 'status', text: 'done', status: 'done' }),
      delta({ id: 3, seq: 3, kind: 'command', text: '$ new' }),
    ];
    const activity = computeStrategicSessionActivity(transcript, []);
    expect(activity.latestLifecycle).toBe('done');
    // The '$ old' activity belongs to the completed turn and is dropped; '$ new'
    // (after the done frame) is the next turn's activity and is kept.
    expect(activity.items.map(i => i.text)).toEqual(['$ new']);
  });

  it('caps the strip to the most recent items', () => {
    const transcript = Array.from({ length: 12 }, (_, i) =>
      delta({ id: i + 1, seq: i + 1, kind: 'command', text: `$ cmd-${i}` }),
    );
    const activity = computeStrategicSessionActivity(transcript, []);
    expect(activity.items).toHaveLength(8);
    expect(activity.items[activity.items.length - 1].text).toBe('$ cmd-11');
  });
});

describe('computeResidentActivityState', () => {
  const NOW = new Date('2026-06-01T12:00:00Z').getTime();
  function session(p: Partial<StrategicResidentSessionSnapshot> = {}): StrategicResidentSessionSnapshot {
    // Fresh heartbeat by default so a session is presumed alive unless overridden.
    return { status: 'active', updated_at: '2026-06-01T11:59:55Z', ...p };
  }

  it('returns null when there is no resident session (nothing to render)', () => {
    expect(computeResidentActivityState([], [], null, NOW)).toBeNull();
    expect(computeResidentActivityState([], [], undefined, NOW)).toBeNull();
  });

  it('EXECUTING: an in-flight command surfaces its text + a live start time', () => {
    const transcript = [
      delta({ id: 1, seq: 1, kind: 'status', text: 'working', status: 'working' }),
      delta({ id: 2, seq: 2, kind: 'command', text: 'bash scripts/test-backend.sh', ts: '2026-06-01T11:59:58Z' }),
    ];
    const state = computeResidentActivityState(transcript, [], session(), NOW);
    expect(state?.phase).toBe('executing');
    expect(state?.commandText).toBe('bash scripts/test-backend.sh');
    // A live start time drives the ticking counter; no frozen endMs in this phase.
    expect(state?.startMs).toBe(new Date('2026-06-01T11:59:58Z').getTime());
    expect(state?.endMs).toBeUndefined();
  });

  it('IDLE: a trailing tool_result means the command finished — no ticking timer', () => {
    const transcript = [
      delta({ id: 1, seq: 1, kind: 'status', text: 'working', status: 'working' }),
      delta({ id: 2, seq: 2, kind: 'command', text: '$ ls' }),
      delta({ id: 3, seq: 3, kind: 'tool_result', text: '✓ done' }),
    ];
    const state = computeResidentActivityState(transcript, [], session(), NOW);
    expect(state?.phase).toBe('idle');
    // No live start time in idle: nothing should tick.
    expect(state?.startMs).toBeUndefined();
  });

  it('IDLE: an active session whose turn is done shows idle, not executing', () => {
    const transcript = [
      delta({ id: 1, seq: 1, kind: 'status', text: 'working', status: 'working' }),
      delta({ id: 2, seq: 2, kind: 'command', text: '$ ran' }),
      delta({ id: 3, seq: 3, kind: 'status', text: 'done', status: 'done' }),
    ];
    const state = computeResidentActivityState(transcript, [], session(), NOW);
    expect(state?.phase).toBe('idle');
  });

  it('DEAD: status ended freezes the counter at ended_at and never ticks', () => {
    const endedAt = '2026-06-01T11:58:00Z';
    const transcript = [
      // A command is still "in flight" per the feed, but the session snapshot says ended.
      delta({ id: 1, seq: 1, kind: 'status', text: 'working', status: 'working' }),
      delta({ id: 2, seq: 2, kind: 'command', text: '$ hung', ts: '2026-06-01T11:57:00Z' }),
    ];
    const state = computeResidentActivityState(
      transcript, [], session({ status: 'ended', ended_at: endedAt }), NOW,
    );
    expect(state?.phase).toBe('dead');
    expect(state?.startMs).toBeUndefined();
    // Frozen at the end time — the counter must not tick against the wall clock.
    expect(state?.endMs).toBe(new Date(endedAt).getTime());
  });

  it('DEAD: stop_requested marks the session gone even without ended_at', () => {
    const state = computeResidentActivityState([], [], session({ stop_requested: true }), NOW);
    expect(state?.phase).toBe('dead');
  });

  it('RECOVERABLE: a stale heartbeat asks for a restart instead of claiming the session ended', () => {
    const staleAt = new Date(NOW - RESIDENT_HEARTBEAT_STALE_MS - 5000).toISOString();
    const transcript = [
      delta({ id: 1, seq: 1, kind: 'status', text: 'working', status: 'working' }),
      delta({ id: 2, seq: 2, kind: 'command', text: '$ hung forever', ts: staleAt }),
    ];
    const state = computeResidentActivityState(transcript, [], session({ updated_at: staleAt }), NOW);
    expect(state?.phase).toBe('dead');
    expect(state?.label).toBe('Workday needs restart');
    expect(state?.label).not.toBe('Session ended');
    // Frozen at the last heartbeat — no forever-ticking counter for a dead session.
    expect(state?.endMs).toBe(new Date(staleAt).getTime());
  });

  it('QUIET CONTINUOUS: stale progress cannot override a fresh process heartbeat', () => {
    const staleProgressAt = new Date(
      NOW - RESIDENT_HEARTBEAT_STALE_MS - 5000,
    ).toISOString();
    const freshHeartbeatAt = new Date(NOW - 3000).toISOString();
    const state = computeResidentActivityState(
      [],
      [],
      session({
        last_progress_at: staleProgressAt,
        worker_heartbeat_at: freshHeartbeatAt,
        updated_at: freshHeartbeatAt,
        activity: {
          phase: 'waiting',
          last_action: 'Watching for project movement',
        },
      }),
      NOW,
    );

    expect(state).toMatchObject({
      phase: 'idle',
      durablePhase: 'waiting',
      label: 'Waiting',
    });
    expect(state?.label).not.toBe('Workday needs restart');
  });

  it('RECONNECTING: an ensured starting worker stays recoverable with a stale prior heartbeat', () => {
    const staleAt = new Date(NOW - RESIDENT_HEARTBEAT_STALE_MS - 5000).toISOString();
    const state = computeResidentActivityState(
      [], [], session({ updated_at: staleAt, worker_state: 'starting' }), NOW,
    );
    expect(state).toMatchObject({ phase: 'idle', label: 'Reconnecting Workday…' });
    expect(state?.label).not.toBe('Session ended');
  });

  it('keeps a long provider retry hold idle, then returns to executing after the hold clears', () => {
    const staleAt = new Date(NOW - RESIDENT_HEARTBEAT_STALE_MS - 10 * 60_000).toISOString();
    const retryUntil = new Date(NOW + 35 * 60_000).toISOString();
    const waiting = computeResidentActivityState(
      [],
      [],
      session({
        updated_at: staleAt,
        worker_state: 'retry_wait',
        worker_retry_until: retryUntil,
        worker_retry_reason: 'Provider rate limit',
      }),
      NOW,
    );
    expect(waiting).toMatchObject({
      phase: 'idle',
      label: 'Provider rate limit — retrying in 35m',
    });
    expect(waiting?.label).not.toBe('Session ended');

    const freshAt = new Date(NOW - 1000).toISOString();
    const resumed = computeResidentActivityState(
      [
        delta({ id: 1, seq: 1, kind: 'status', text: 'working', status: 'working' }),
        delta({ id: 2, seq: 2, kind: 'command', text: '$ resumed', ts: freshAt }),
      ],
      [],
      session({ updated_at: freshAt, worker_state: 'ready' }),
      NOW,
    );
    expect(resumed).toMatchObject({ phase: 'executing', commandText: '$ resumed' });
  });

  it('stays EXECUTING while the heartbeat is fresh and a command is in flight', () => {
    const freshAt = new Date(NOW - 3000).toISOString();
    const transcript = [
      delta({ id: 1, seq: 1, kind: 'status', text: 'working', status: 'working' }),
      delta({ id: 2, seq: 2, kind: 'command', text: '$ running', ts: freshAt }),
    ];
    const state = computeResidentActivityState(transcript, [], session({ updated_at: freshAt }), NOW);
    expect(state?.phase).toBe('executing');
  });

  it('prefers the durable activity contract and carries idle/message/watch detail', () => {
    const state = computeResidentActivityState(
      [],
      [],
      session({
        activity: {
          phase: 'waiting',
          phase_started_at: '2026-06-01T11:59:00Z',
          last_action: 'Watching task completions',
          reason_code: 'consultation_queued',
          consultation_id: '11111111-1111-4111-8111-111111111111',
          autonomy_state: 'paused',
          autonomy_reason_code: 'consultation_queued',
          pending_operator_messages: 2,
          idle_remaining_seconds: 3540,
          pending_watches: 1,
          fired_watches: 2,
        },
      }),
      NOW,
    );
    expect(state).toMatchObject({
      phase: 'idle',
      durablePhase: 'waiting',
      label: 'Waiting',
      detail: 'Watching task completions',
      reasonCode: 'consultation_queued',
      consultationId: '11111111-1111-4111-8111-111111111111',
      autonomyState: 'paused',
      autonomyReasonCode: 'consultation_queued',
      idleRemainingSeconds: 3540,
      pendingOperatorMessages: 2,
      pendingWatches: 1,
      firedWatches: 2,
    });
  });

  it('renders the durable pre-stop warning as non-ticking stopping state', () => {
    const state = computeResidentActivityState(
      [],
      [],
      session({
        activity: {
          phase: 'stopping',
          last_action: 'Idle limit approaching',
          idle_remaining_seconds: 240,
        },
      }),
      NOW,
    );
    expect(state).toMatchObject({
      phase: 'idle',
      durablePhase: 'stopping',
      label: 'Stopping soon',
      idleRemainingSeconds: 240,
    });
    expect(state?.startMs).toBeUndefined();
  });
});

describe('countStrategicReplies', () => {
  it('counts strategic/strategist from_type messages', () => {
    expect(countStrategicReplies([strategicMsg('2026-06-01T10:00:00Z'), strategicMsg('2026-06-01T10:01:00Z')])).toBe(2);
  });
});

describe('scopeStrategicTranscript', () => {
  it('keeps only deltas stamped with the selected project', () => {
    const transcript = [
      delta({ id: 1, project_id: 'p1', kind: 'message', text: 'mine' }),
      delta({ id: 2, project_id: 'p2', kind: 'message', text: 'theirs' }),
    ];
    expect(scopeStrategicTranscript(transcript, 'p1').map(d => d.text)).toEqual(['mine']);
    expect(scopeStrategicTranscript(transcript, 'p2').map(d => d.text)).toEqual(['theirs']);
  });

  it('drops unattributable deltas rather than letting them leak across projects', () => {
    const transcript = [delta({ id: 1, kind: 'message', text: 'no project' })];
    expect(scopeStrategicTranscript(transcript, 'p1')).toEqual([]);
  });

  it('renders nothing when no project is selected, and tolerates an absent buffer', () => {
    const transcript = [delta({ id: 1, project_id: 'p1', kind: 'message', text: 'mine' })];
    expect(scopeStrategicTranscript(transcript, null)).toEqual([]);
    expect(scopeStrategicTranscript(transcript, undefined)).toEqual([]);
    expect(scopeStrategicTranscript(undefined, 'p1')).toEqual([]);
  });
});

describe('computeStrategicChatLiveState', () => {
  const noActivity: StrategicLiveActivity = { items: [], latestLifecycle: null };

  it('is null when nothing is in flight — a poll-only turn shows no affordance', () => {
    expect(computeStrategicChatLiveState(null, noActivity)).toBeNull();
  });

  it('reports the streaming phase with the newest activity one-liner as detail', () => {
    const turn = computeStrategicStreamingTurn(
      [delta({ id: 1, seq: 1, kind: 'message', text: 'partial answer' })],
      [],
    );
    const activity = computeStrategicSessionActivity(
      [
        delta({ id: 1, seq: 1, status: 'responding' }),
        delta({ id: 2, seq: 2, kind: 'command', text: 'pytest testing/test_release_scoring.py' }),
      ],
      [],
    );
    expect(computeStrategicChatLiveState(turn, activity)).toEqual({
      phase: 'streaming',
      label: 'Replying',
      detail: 'pytest testing/test_release_scoring.py',
    });
  });

  it('carries the newest valid timestamp from a no-Resident Chat turn', () => {
    const transcript = [
      delta({ id: 1, seq: 1, status: 'responding', ts: '2026-06-01T11:59:00Z' }),
      delta({ id: 2, seq: 2, kind: 'command', text: 'pytest tests/chat.py', ts: 'not-a-timestamp' }),
      delta({ id: 3, seq: 3, kind: 'message', text: 'partial answer', ts: '2026-06-01T12:00:00Z' }),
    ];
    const turn = computeStrategicStreamingTurn(transcript, []);
    const activity = computeStrategicSessionActivity(transcript, []);
    const state = computeStrategicChatLiveState(turn, activity, Date.parse('2026-06-01T12:00:01Z'));

    expect(activity.lastActivityMs).toBe(Date.parse('2026-06-01T12:00:00Z'));
    expect(state).toMatchObject({
      phase: 'streaming',
      detail: 'pytest tests/chat.py',
      lastActivityMs: Date.parse('2026-06-01T12:00:00Z'),
    });
  });

  it('reports the working phase when the turn is open but no reply text has streamed', () => {
    const activity = computeStrategicSessionActivity(
      [
        delta({ id: 1, seq: 1, status: 'working' }),
        delta({ id: 2, seq: 2, kind: 'read', text: 'read release_scoring.py' }),
      ],
      [],
    );
    expect(computeStrategicChatLiveState(null, activity)).toEqual({
      phase: 'working',
      label: 'Working',
      detail: 'read release_scoring.py',
    });
  });

  it('reports durable active work when the live feed is empty after reload', () => {
    const strategic = durableStrategicState({
      task: { title: 'Review the strategic direction' },
    });

    expect(computeStrategicChatLiveState(null, noActivity, strategic)).toEqual({
      phase: 'working',
      label: 'Working',
      detail: 'Review the strategic direction',
      lastActivityMs: Date.parse('2026-06-01T11:30:00Z'),
    });
  });

  it('reports a stuck chat turn when the durable cycle is active with no live task and the live feed is silent', () => {
    const strategic = durableStrategicState({
      cycle: { has_active_task: false, stale: true },
      task: null,
    });

    expect(computeStrategicChatLiveState(null, noActivity, strategic)).toEqual({
      phase: 'stuck',
      label: 'Stuck',
      detail: 'Strategic review',
      lastActivityMs: Date.parse('2026-06-01T11:00:00Z'),
    });
  });

  it('prefers live activity timestamps, then durable task timestamps over cycle start', () => {
    const strategic = durableStrategicState({
      task: { updated_at: '2026-06-01T11:20:00Z' },
    });
    const liveActivity: StrategicLiveActivity = {
      items: [],
      latestLifecycle: 'working',
      lastActivityMs: Date.parse('2026-06-01T11:40:00Z'),
    };

    expect(computeStrategicChatLiveState(null, liveActivity, strategic)?.lastActivityMs)
      .toBe(Date.parse('2026-06-01T11:40:00Z'));
    expect(computeStrategicChatLiveState(null, noActivity, strategic)?.lastActivityMs)
      .toBe(Date.parse('2026-06-01T11:20:00Z'));
  });

  it('clears once the turn closes with a done frame', () => {
    const transcript = [
      delta({ id: 1, seq: 1, status: 'responding' }),
      delta({ id: 2, seq: 2, kind: 'message', text: 'answer' }),
      delta({ id: 3, seq: 3, status: 'done' }),
    ];
    const turn = computeStrategicStreamingTurn(transcript, []);
    const activity = computeStrategicSessionActivity(transcript, []);
    expect(turn).toBeNull();
    expect(computeStrategicChatLiveState(turn, activity)).toEqual({
      phase: 'idle',
      label: 'Chat ready',
    });
  });

  it('preserves the completed lifecycle timestamp for the idle age stamp', () => {
    const completedAt = '2026-06-01T12:00:00Z';
    const transcript = [
      delta({ id: 1, seq: 1, status: 'responding', ts: '2026-06-01T11:59:00Z' }),
      delta({ id: 2, seq: 2, status: 'done', ts: completedAt }),
    ];
    const activity = computeStrategicSessionActivity(transcript, []);

    expect(activity.lastActivityMs).toBe(Date.parse(completedAt));
    expect(computeStrategicChatLiveState(null, activity, Date.parse('2026-06-01T12:00:30Z'))).toEqual({
      phase: 'idle',
      label: 'Chat ready',
      lastActivityMs: Date.parse(completedAt),
    });
  });

  it('computeStrategicChatLiveState reports stale once the injected clock passes the chat staleness threshold', () => {
    const lastActivityMs = Date.parse('2026-06-01T12:00:00Z');
    const transcript = [
      delta({ id: 1, seq: 1, status: 'responding', ts: new Date(lastActivityMs).toISOString() }),
      delta({ id: 2, seq: 2, kind: 'command', text: 'waiting for provider', ts: new Date(lastActivityMs).toISOString() }),
    ];
    const activity = computeStrategicSessionActivity(transcript, []);

    expect(computeStrategicChatLiveState(
      null,
      activity,
      lastActivityMs + CHAT_LIVE_STALE_MS + 1,
    )).toMatchObject({
      phase: 'stale',
      label: 'Chat stalled',
      lastActivityMs,
    });
  });

  it('does not invent an age or stale state for an untimestamped completed turn', () => {
    const activity = computeStrategicSessionActivity([
      delta({ id: 1, seq: 1, status: 'responding', ts: 'not-a-timestamp' }),
      delta({ id: 2, seq: 2, status: 'done', ts: 'also-not-a-timestamp' }),
    ], []);

    expect(computeStrategicChatLiveState(null, activity, Date.now())).toEqual({
      phase: 'idle',
      label: 'Chat ready',
    });
  });

  it('elides an over-long activity detail instead of blowing out the header', () => {
    const long = 'x'.repeat(200);
    const activity = computeStrategicSessionActivity(
      [delta({ id: 1, seq: 1, status: 'working' }), delta({ id: 2, seq: 2, kind: 'command', text: long })],
      [],
    );
    const state = computeStrategicChatLiveState(null, activity);
    expect(state?.detail).toHaveLength(60);
    expect(state?.detail?.endsWith('…')).toBe(true);
  });
});
