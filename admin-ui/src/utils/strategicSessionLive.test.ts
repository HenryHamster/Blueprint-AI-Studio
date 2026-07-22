import { describe, it, expect } from 'vitest';
import {
  computeStrategicStreamingTurn,
  computeStrategicSessionActivity,
  countStrategicReplies,
  isStrategicReplyDelta,
} from './strategicSessionLive';
import type { StrategicSessionDelta } from '../hooks/useLogViewer';
import type { ProjectMessage } from '../types/messages';

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

describe('countStrategicReplies', () => {
  it('counts strategic/strategist from_type messages', () => {
    expect(countStrategicReplies([strategicMsg('2026-06-01T10:00:00Z'), strategicMsg('2026-06-01T10:01:00Z')])).toBe(2);
  });
});
