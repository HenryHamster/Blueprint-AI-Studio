import { describe, it, expect } from 'vitest';
import {
  splitStreamChunk, extractDeltaText, isStrategicMutation, isMessageSend,
  parseResidentLine, encodeUserTurn, encodeInterrupt, extractResultUsage,
} from './resident-stream.js';

describe('splitStreamChunk', () => {
  it('returns complete lines and carries a trailing partial', () => {
    const r = splitStreamChunk('', '{"a":1}\n{"b":2}\n{"c":');
    expect(r.lines).toEqual(['{"a":1}', '{"b":2}']);
    expect(r.carry).toBe('{"c":');
  });

  it('prepends the prior carry to the next chunk', () => {
    const first = splitStreamChunk('', '{"a":');
    expect(first.lines).toEqual([]);
    expect(first.carry).toBe('{"a":');
    const second = splitStreamChunk(first.carry, '1}\n');
    expect(second.lines).toEqual(['{"a":1}']);
    expect(second.carry).toBe('');
  });

  it('drops empty lines between newlines', () => {
    const r = splitStreamChunk('', 'a\n\n\nb\n');
    expect(r.lines).toEqual(['a', 'b']);
  });
});

describe('extractDeltaText', () => {
  it('extracts text_delta content', () => {
    const obj = { type: 'stream_event', event: { type: 'content_block_delta', delta: { type: 'text_delta', text: 'Hi' } } };
    expect(extractDeltaText(obj)).toBe('Hi');
  });

  it('ignores thinking_delta', () => {
    const obj = { event: { type: 'content_block_delta', delta: { type: 'thinking_delta', thinking: 'hmm' } } };
    expect(extractDeltaText(obj)).toBeUndefined();
  });

  it('ignores non-delta events', () => {
    expect(extractDeltaText({ event: { type: 'message_start' } })).toBeUndefined();
    expect(extractDeltaText({})).toBeUndefined();
  });
});

describe('isStrategicMutation', () => {
  it('detects mutating ./strategic verbs', () => {
    expect(isStrategicMutation('./strategic set-problem-model "$(cat /tmp/x.json)"')).toBe(true);
    expect(isStrategicMutation('./strategic create-direction \'{"text":"x"}\'')).toBe(true);
    expect(isStrategicMutation('strategic retire-direction \'{}\'')).toBe(true);
  });

  it('does not count reads (fetch / design-doc get) as mutations', () => {
    expect(isStrategicMutation('./fetch problem-model')).toBe(false);
    expect(isStrategicMutation('./design-doc get systems/auth')).toBe(false);
    expect(isStrategicMutation('cat /tmp/foo')).toBe(false);
    expect(isStrategicMutation(undefined)).toBe(false);
  });

  it('detects a strategic op mid-pipeline', () => {
    expect(isStrategicMutation('cd /tmp && ./strategic spawn-task \'{}\'')).toBe(true);
  });
});

describe('isMessageSend', () => {
  it('detects ./messages send in various shells', () => {
    expect(isMessageSend("./messages send 'hi there'")).toBe(true);
    expect(isMessageSend('messages send "answer" QUESTION')).toBe(true);
    expect(isMessageSend('cd /x && ./messages send \'done\'')).toBe(true);
  });

  it('does not count resolve / unrelated commands', () => {
    expect(isMessageSend('./messages resolve abc123 \'fixed\'')).toBe(false);
    expect(isMessageSend('./strategic spawn-task \'{}\'')).toBe(false);
    expect(isMessageSend(undefined)).toBe(false);
  });
});

describe('extractResultUsage', () => {
  it('folds cache classes into inputTokens and breaks them out', () => {
    const u = extractResultUsage({
      usage: {
        input_tokens: 100, output_tokens: 40,
        cache_creation_input_tokens: 10, cache_read_input_tokens: 5,
      },
    });
    expect(u).toEqual({ inputTokens: 115, outputTokens: 40, cacheCreationTokens: 10, cacheReadTokens: 5 });
  });

  it('returns undefined when usage is absent or all-zero', () => {
    expect(extractResultUsage({})).toBeUndefined();
    expect(extractResultUsage({ usage: { input_tokens: 0, output_tokens: 0 } })).toBeUndefined();
  });
});

describe('parseResidentLine', () => {
  it('classifies a text delta', () => {
    const events = parseResidentLine(JSON.stringify({
      type: 'stream_event', session_id: 's1',
      event: { type: 'content_block_delta', delta: { type: 'text_delta', text: 'hello' } },
    }));
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ kind: 'text', text: 'hello', sessionId: 's1' });
    expect(events[0].isAssistantMessage).toBeFalsy();
  });

  it('classifies a finalized assistant text block', () => {
    const events = parseResidentLine(JSON.stringify({
      type: 'assistant', message: { content: [{ type: 'text', text: 'done' }] },
    }));
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ kind: 'text', text: 'done', isAssistantMessage: true });
  });

  it('classifies a Bash tool_use and carries the command + tool_use id', () => {
    const events = parseResidentLine(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', id: 'toolu_42', name: 'Bash', input: { command: './strategic create-direction \'{}\'' } }] },
    }));
    expect(events[0]).toMatchObject({ kind: 'tool', toolName: 'Bash', toolUseId: 'toolu_42' });
    expect(events[0].command).toContain('create-direction');
  });

  it('classifies a tool_result and carries its tool_use_id + is_error', () => {
    const ok = parseResidentLine(JSON.stringify({
      type: 'user',
      message: { content: [{ type: 'tool_result', tool_use_id: 'toolu_42', content: 'sent', is_error: false }] },
    }));
    expect(ok).toHaveLength(1);
    expect(ok[0]).toMatchObject({ kind: 'tool_result', toolUseId: 'toolu_42', isError: false });

    const failed = parseResidentLine(JSON.stringify({
      type: 'user',
      message: { content: [{ type: 'tool_result', tool_use_id: 'toolu_99', content: 'curl: (7) failed', is_error: true }] },
    }));
    expect(failed[0]).toMatchObject({ kind: 'tool_result', toolUseId: 'toolu_99', isError: true });
  });

  it('treats a tool_result-less user line as bare system noise', () => {
    const events = parseResidentLine(JSON.stringify({ type: 'user', message: { content: [] }, session_id: 's3' }));
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ kind: 'system', sessionId: 's3' });
  });

  it('marks a top-level result as a turn result', () => {
    const events = parseResidentLine(JSON.stringify({ type: 'result', result: 'final answer', session_id: 's9' }));
    expect(events[0]).toMatchObject({ kind: 'result', text: 'final answer', isTurnResult: true, sessionId: 's9' });
  });

  it('carries token usage on a result event', () => {
    const events = parseResidentLine(JSON.stringify({
      type: 'result', result: 'done',
      usage: { input_tokens: 10, output_tokens: 3, cache_read_input_tokens: 2 },
    }));
    expect(events[0].usage).toEqual({
      inputTokens: 12, outputTokens: 3, cacheCreationTokens: 0, cacheReadTokens: 2,
    });
  });

  it('does NOT mark a nested result as a turn result', () => {
    const events = parseResidentLine(JSON.stringify({ type: 'result', result: 'sub', parent_tool_use_id: 'tool_1' }));
    expect(events[0]).toMatchObject({ kind: 'result', nested: true });
    expect(events[0].isTurnResult).toBe(false);
  });

  it('captures session_id from the system init event', () => {
    const events = parseResidentLine(JSON.stringify({ type: 'system', subtype: 'init', session_id: 'sess-abc' }));
    expect(events[0]).toMatchObject({ kind: 'system', sessionId: 'sess-abc' });
  });

  it('surfaces a structured error message', () => {
    const events = parseResidentLine(JSON.stringify({ type: 'error', error: { message: 'boom' } }));
    expect(events[0]).toMatchObject({ kind: 'error', text: 'boom' });
  });

  it('returns an other event for non-JSON lines', () => {
    const events = parseResidentLine('plain log line');
    expect(events[0]).toMatchObject({ kind: 'other', text: 'plain log line' });
  });

  it('returns nothing for blank lines', () => {
    expect(parseResidentLine('   ')).toEqual([]);
  });

  it('splits an assistant message with both text and tool_use into two events', () => {
    const events = parseResidentLine(JSON.stringify({
      type: 'assistant',
      message: { content: [
        { type: 'text', text: 'let me check' },
        { type: 'tool_use', name: 'Bash', input: { command: 'ls' } },
      ] },
    }));
    expect(events).toHaveLength(2);
    expect(events[0].kind).toBe('text');
    expect(events[1].kind).toBe('tool');
  });
});

describe('encodeUserTurn / encodeInterrupt', () => {
  it('encodes a user turn as a single NDJSON line', () => {
    const line = encodeUserTurn('hi there');
    expect(line.endsWith('\n')).toBe(true);
    const obj = JSON.parse(line.trim()) as Record<string, unknown>;
    expect(obj).toEqual({ type: 'user', message: { role: 'user', content: [{ type: 'text', text: 'hi there' }] } });
  });

  it('encodes an interrupt control_request with a request id', () => {
    const line = encodeInterrupt('req-1');
    const obj = JSON.parse(line.trim()) as Record<string, unknown>;
    expect(obj).toEqual({ type: 'control_request', request_id: 'req-1', request: { subtype: 'interrupt' } });
  });
});
