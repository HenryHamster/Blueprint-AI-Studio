import { describe, it, expect } from 'vitest';
import {
  splitStreamChunk, extractDeltaText, isStrategicMutation, isMessageSend,
  parseResidentLine, encodeUserTurn, encodeUserTurnBlocks, encodeInterrupt,
  extractResultUsage, extractResultTurns, describeToolUse, describeToolResult,
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
    expect(isStrategicMutation('../strategic create-direction \'{}\'')).toBe(true);
    expect(isStrategicMutation('/workspace/strategic update-direction \'{}\'')).toBe(true);
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

  it('detects ops previously missing from the hand-maintained set', () => {
    expect(isStrategicMutation('./strategic activate-direction d-abc123')).toBe(true);
    expect(isStrategicMutation('./strategic dispose-completion-debt cd-abc123')).toBe(true);
  });

  it('rejects internal-only ops that are not CLI-surfaced', () => {
    expect(isStrategicMutation('./strategic cite-memos \'{}\'')).toBe(false);
    expect(isStrategicMutation('./strategic set-strategic-challenge \'{}\'')).toBe(false);
  });
});

describe('isMessageSend', () => {
  it('detects ./messages send in various shells', () => {
    expect(isMessageSend("./messages send 'hi there'")).toBe(true);
    expect(isMessageSend('messages send "answer" QUESTION')).toBe(true);
    expect(isMessageSend('cd /x && ./messages send \'done\'')).toBe(true);
    expect(isMessageSend('../messages send \'from a subdir\'')).toBe(true);
    expect(isMessageSend('/workspace/messages send \'absolute helper\'')).toBe(true);
  });

  it('also detects ./strategic create-message (double-post guard)', () => {
    expect(isMessageSend('./strategic create-message \'{"type":"NOTE","content":"hi"}\'')).toBe(true);
    expect(isMessageSend('cd /ws && strategic create-message \'{}\'')).toBe(true);
    expect(isMessageSend('../strategic create-message \'{}\'')).toBe(true);
    expect(isMessageSend('/workspace/strategic create-message \'{}\'')).toBe(true);
  });

  it('does not count resolve / unrelated commands', () => {
    expect(isMessageSend('./messages resolve abc123 \'fixed\'')).toBe(false);
    expect(isMessageSend('./strategic spawn-task \'{}\'')).toBe(false);
    expect(isMessageSend('./strategic resolve-message ref-1 \'done\'')).toBe(false);
    expect(isMessageSend(undefined)).toBe(false);
  });
});

describe('test_message_ops_not_lease_renewing_mutations', () => {
  it('create-message and resolve-message do not count as lease-renewing mutations', () => {
    // Posting a message is operator communication, not a strategic-state mutation;
    // using it must never renew the authority lease (dead-man's-switch contract).
    expect(isStrategicMutation('./strategic create-message \'{"type":"NOTE","content":"hi"}\'')).toBe(false);
    expect(isStrategicMutation('./strategic resolve-message ref-abc \'resolved\'')).toBe(false);
    // Confirm that real strategic mutations still trigger the lease renewal.
    expect(isStrategicMutation('./strategic create-direction \'{}\'')).toBe(true);
    expect(isStrategicMutation('./strategic spawn-task \'{}\'')).toBe(true);
    expect(isStrategicMutation('./strategic set-problem-model /tmp/pm.json')).toBe(true);
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

describe('extractResultTurns', () => {
  it('returns a floored positive turn count and ignores invalid values', () => {
    expect(extractResultTurns({ num_turns: 3.9 })).toBe(3);
    expect(extractResultTurns({ num_turns: 0 })).toBeUndefined();
    expect(extractResultTurns({ num_turns: Number.NaN })).toBeUndefined();
  });
});

describe('describeToolUse', () => {
  it('maps Bash to a $-prefixed command (first line only, capped)', () => {
    expect(describeToolUse('Bash', { command: 'ls -la' })).toEqual({ kind: 'command', text: '$ ls -la' });
    expect(describeToolUse('Bash', { command: 'a\nb' })).toEqual({ kind: 'command', text: '$ a' });
    // Over-long command is truncated with an ellipsis.
    const long = 'x'.repeat(300);
    const out = describeToolUse('Bash', { command: long });
    expect(out?.kind).toBe('command');
    expect((out?.text.length ?? 0)).toBeLessThanOrEqual(161);
    expect(out?.text.endsWith('…')).toBe(true);
  });

  it('drops an empty Bash command (nothing to surface)', () => {
    expect(describeToolUse('Bash', { command: '   ' })).toBeUndefined();
    expect(describeToolUse('Bash', {})).toBeUndefined();
  });

  it('maps Edit/Write/Read/Task to their activity kinds', () => {
    expect(describeToolUse('Write', { file_path: '/a/b/c.ts' })).toEqual({ kind: 'file', text: 'wrote …/b/c.ts' });
    expect(describeToolUse('Read', { file_path: 'x.md' })).toEqual({ kind: 'read', text: 'read x.md' });
    expect(describeToolUse('Task', { subagent_type: 'Plan', description: 'design it' }))
      .toEqual({ kind: 'subagent', text: 'spawned Plan: design it' });
  });

  it('falls back to the bare tool name for any other tool', () => {
    expect(describeToolUse('Grep', { pattern: 'x' })).toEqual({ kind: 'tool', text: 'Grep' });
  });
});

describe('describeToolResult', () => {
  it('summarizes success and error with a ✓/✗ prefix', () => {
    expect(describeToolResult({ content: 'ok', is_error: false })).toEqual({ kind: 'tool_result', text: '✓ ok' });
    expect(describeToolResult({ content: 'boom', is_error: true })).toEqual({ kind: 'tool_result', text: '✗ boom' });
    expect(describeToolResult({ is_error: false })).toEqual({ kind: 'tool_result', text: '✓ done' });
    expect(describeToolResult({ is_error: true })).toEqual({ kind: 'tool_result', text: '✗ tool error' });
  });

  it('reads a text block out of an array-content tool_result', () => {
    const out = describeToolResult({ content: [{ type: 'text', text: 'line1\nline2' }], is_error: false });
    expect(out.text).toBe('✓ line1');
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

  it('carries a compact activity one-liner on a Bash tool_use', () => {
    const events = parseResidentLine(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', id: 't1', name: 'Bash', input: { command: 'npm test\nsecond line' } }] },
    }));
    expect(events[0]).toMatchObject({ kind: 'tool', activityKind: 'command', activityText: '$ npm test' });
  });

  it('carries a file-edit / read / subagent activity one-liner per tool', () => {
    const edit = parseResidentLine(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', id: 't2', name: 'Edit', input: { file_path: '/repo/app/main.py' } }] },
    }));
    expect(edit[0]).toMatchObject({ activityKind: 'file', activityText: 'edited …/app/main.py' });

    const read = parseResidentLine(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', id: 't3', name: 'Read', input: { file_path: 'README.md' } }] },
    }));
    expect(read[0]).toMatchObject({ activityKind: 'read', activityText: 'read README.md' });

    const task = parseResidentLine(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', id: 't4', name: 'Task', input: { subagent_type: 'Explore', description: 'map the auth flow' } }] },
    }));
    expect(task[0]).toMatchObject({ activityKind: 'subagent', activityText: 'spawned Explore: map the auth flow' });
  });

  it('carries a tool_result summary one-liner (success + error)', () => {
    const ok = parseResidentLine(JSON.stringify({
      type: 'user',
      message: { content: [{ type: 'tool_result', tool_use_id: 't1', content: 'created direction d-1', is_error: false }] },
    }));
    expect(ok[0]).toMatchObject({ kind: 'tool_result', activityKind: 'tool_result' });
    expect(ok[0].activityText).toBe('✓ created direction d-1');

    const bad = parseResidentLine(JSON.stringify({
      type: 'user',
      message: { content: [{ type: 'tool_result', tool_use_id: 't9', content: 'curl: (7) failed', is_error: true }] },
    }));
    expect(bad[0].activityText).toBe('✗ curl: (7) failed');
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

  it('marks a structured terminal api_error as failed even when subtype says success', () => {
    const events = parseResidentLine(JSON.stringify({
      type: 'result',
      subtype: 'success',
      terminal_reason: 'api_error',
      api_error_status: 403,
      result: 'Your organization has disabled Claude subscription access for Claude Code',
    }));
    expect(events[0]).toMatchObject({
      kind: 'result',
      isError: true,
      isTurnResult: true,
      terminalReason: 'api_error',
      apiErrorStatus: 403,
    });
  });

  it('marks aborted_streaming as failed when the provider omits is_error', () => {
    const events = parseResidentLine(JSON.stringify({
      type: 'result',
      subtype: 'success',
      terminal_reason: 'aborted_streaming',
      result: 'partial output',
    }));
    expect(events[0]).toMatchObject({
      kind: 'result',
      isError: true,
      isTurnResult: true,
      terminalReason: 'aborted_streaming',
    });
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

  it('carries num_turns on a result event', () => {
    const events = parseResidentLine(JSON.stringify({
      type: 'result', result: 'done', num_turns: 4,
    }));
    expect(events[0].numTurns).toBe(4);
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

describe('encodeUserTurn / encodeInterrupt / encodeUserTurnBlocks', () => {
  it('encodes a user turn as a single NDJSON line', () => {
    const line = encodeUserTurn('hi there');
    expect(line.endsWith('\n')).toBe(true);
    const obj = JSON.parse(line.trim()) as Record<string, unknown>;
    expect(obj).toEqual({ type: 'user', message: { role: 'user', content: [{ type: 'text', text: 'hi there' }] } });
  });

  it('encodes a user turn with multiple content blocks (text + image)', () => {
    const blocks: Array<Record<string, unknown>> = [
      { type: 'text', text: 'see this image' },
      { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'iVBORw0KGgo=' } },
    ];
    const line = encodeUserTurnBlocks(blocks);
    expect(line.endsWith('\n')).toBe(true);
    const obj = JSON.parse(line.trim()) as Record<string, unknown>;
    expect(obj).toEqual({
      type: 'user',
      message: { role: 'user', content: blocks },
    });
  });

  it('encodes an interrupt control_request with a request id', () => {
    const line = encodeInterrupt('req-1');
    const obj = JSON.parse(line.trim()) as Record<string, unknown>;
    expect(obj).toEqual({ type: 'control_request', request_id: 'req-1', request: { subtype: 'interrupt' } });
  });
});
