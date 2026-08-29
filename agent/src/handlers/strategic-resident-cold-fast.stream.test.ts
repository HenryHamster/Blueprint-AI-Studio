import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createHash } from 'crypto';
import type { ResidentSessionEvent } from '../providers/resident-session.js';
import { ConversationMessageStreamer, STREAM_DELTA_MAX_CHARS } from './conversation-message-streamer.js';

// ── Hoisted test doubles ─────────────────────────────────────────────────────
const mocks = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  sendLog: vi.fn(),
  sendWsFrame: vi.fn((_ws: unknown, _frame: unknown) => true),
  dispatch: vi.fn(),
  ensureReady: vi.fn(() => Promise.resolve()),
  isAlive: vi.fn(() => true),
  interrupt: vi.fn(),
  close: vi.fn(),
  replaceAfterCrash: vi.fn(),
  /** Subscribers the cold-fast lane registered on its controller. */
  listeners: [] as Array<(ev: ResidentSessionEvent) => void>,
}));

const emit = (ev: ResidentSessionEvent): void => {
  for (const listener of mocks.listeners) listener(ev);
};

vi.mock('../index.js', () => ({
  apiGet: mocks.apiGet,
  apiPost: mocks.apiPost,
  sanitize: (value: string) => value,
  sendLog: mocks.sendLog,
  sendWsFrame: mocks.sendWsFrame,
}));

vi.mock('../providers/foreground-resident.js', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../providers/foreground-resident.js')>()),
  typedFastRuntimeConfig: () => ({
    activated: true,
    adapter: { id: 'stub', validation: 'qualified', start: () => { throw new Error('unused'); } },
    model: 'stub-model',
    effort: 'medium',
  }),
}));

vi.mock('./strategic-context-envelope.js', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./strategic-context-envelope.js')>()),
  verifyStrategicContextEnvelope: (envelope: { transcript_sequence: number }) => ({ envelope }),
}));

vi.mock('./typed-fast-resident.js', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./typed-fast-resident.js')>()),
  ForegroundTypedController: class {
    onEvent(cb: (ev: ResidentSessionEvent) => void): void { mocks.listeners.push(cb); }
    ensureReady(): Promise<void> { return mocks.ensureReady(); }
    dispatch(...args: unknown[]): unknown { return mocks.dispatch(...args); }
    isAlive(): boolean { return mocks.isAlive(); }
    interrupt(): void { mocks.interrupt(); }
    replaceAfterCrash(): void { mocks.replaceAfterCrash(); }
    close(): void { mocks.close(); }
  },
  newTypedConversationReceipt: (claimId: string, messageIds: string[]) => ({
    claim_id: claimId, message_ids: messageIds, idempotency_key: `typed-fast:${claimId}`,
  }),
  typedFastAbandonDispositionWithConfirmation: () => 'fallback',
  typedFastFailureBackoffMs: () => 1,
}));

import {
  assertSelectedMessagesMatchEnvelope,
  buildTypedFastDispatchPrompt,
  canonicalAttachmentManifest,
  runCheckoutIndependentFastStartup,
} from './strategic-resident-cold-fast.js';

// ── Fixtures ─────────────────────────────────────────────────────────────────
const MESSAGE_ID = 'msg-1';

const turnResult = (overrides: Record<string, unknown> = {}) => ({
  resultText: '', accumulatedTurnText: '', interrupted: false,
  timedOut: false, hardTimedOut: false, appliedStrategicOp: false,
  sawToolUse: false, sentOperatorMessage: false, operatorReply: null,
  processGone: false, ttftMs: null, turnMs: 1, toolCallCount: 0,
  totalToolCallDurationMs: 0, meanInterTokenMs: null, inputTokens: 1,
  providerFailed: false, failureCategory: null,
  ...overrides,
});

const envelope = {
  transcript_sequence: 7,
  context_authority_generation: 1,
  privacy_generation: 1,
  recent_conversation: [{
    sequence: 7,
    message_id: MESSAGE_ID,
    from_type: 'user',
    content: 'hi',
    content_bytes: 2,
    content_sha256: createHash('sha256').update('hi').digest('hex'),
    attachment_manifest: [],
  }],
};

const startOptions = (ws: unknown) => ({
  taskId: 'task-1',
  payload: { typed_fast_enabled: true },
  task: { context: { message_cursor: '2026-08-19T00:00:00Z' } },
  projectId: 'proj-1',
  sessionId: 'sess-1',
  workerGeneration: 3,
  residentIncarnationId: 'inc-1',
  workspacePreparation: Promise.resolve(),
  initialAdmission: {} as never,
  initialAdmissionAt: Date.now(),
  ws: ws as never,
  statusRef: { value: 'Starting resident strategic session...' },
});

/** Every `strategic_session` frame the lane pushed at the socket, in order. */
const sessionFrames = (): Array<{ kind: string; text: string; status?: string; seq: number }> =>
  mocks.sendWsFrame.mock.calls
    .map(([, frame]) => frame as { type: string; payload: Record<string, unknown> })
    .filter(frame => frame.type === 'strategic_session')
    .map(frame => frame.payload as unknown as { kind: string; text: string; status?: string; seq: number });

const conversationFrames = (): Array<{ content: string; seq: number }> =>
  mocks.sendWsFrame.mock.calls
    .map(([, frame]) => frame as { type: string; payload: Record<string, unknown> })
    .filter(frame => frame.type === 'conversation_message_delta')
    .map(frame => frame.payload as unknown as { content: string; seq: number });

const completeCalls = (): unknown[][] =>
  mocks.apiPost.mock.calls.filter(([url]) => String(url).endsWith('/conversation/turns/complete'));

beforeEach(() => {
  vi.clearAllMocks();
  mocks.listeners.length = 0;
  mocks.sendWsFrame.mockReturnValue(true);
  mocks.isAlive.mockReturnValue(true);
  mocks.ensureReady.mockResolvedValue(undefined);
  mocks.apiGet.mockResolvedValue({
    messages: [{
      id: MESSAGE_ID, from_type: 'user', content: 'hi', created_at: '2026-08-19T00:00:01Z',
    }],
    fast_message_ids: [MESSAGE_ID],
  });
  mocks.apiPost.mockImplementation((url: string) => {
    if (url.endsWith('/typed-fast-lease/acquire')) {
      return Promise.resolve({ acquired: true, lease: { lease_id: 'lease-1', generation: 1 } });
    }
    if (url.endsWith('/conversation/turns/claim')) {
      return Promise.resolve({
        claim_id: 'claim-1', owner: 'fast', message_ids: [MESSAGE_ID], envelope,
      });
    }
    return Promise.resolve({});
  });
});

afterEach(() => { vi.useRealTimers(); });

/** The streamer drains at <=10 frames/s, so a turn's frames land after the run
 *  returns. Wait for the terminating `done` rather than a fixed sleep. */
const waitForDone = async (): Promise<void> => {
  await vi.waitFor(() => {
    expect(sessionFrames().some(frame => frame.status === 'done')).toBe(true);
  }, { timeout: 5_000, interval: 20 });
};

describe('typed Fast active-input integrity', () => {
  const row = (id: string, sequence: number, content: string) => ({
    sequence,
    message_id: id,
    from_type: 'user',
    content: content.slice(0, 2_048),
    content_truncated: content.length > 2_048,
    content_bytes: Buffer.byteLength(content, 'utf8'),
    content_sha256: createHash('sha256').update(content, 'utf8').digest('hex'),
    attachment_manifest: [],
  });
  const verified = (rows: Array<Record<string, unknown>>, sequence: number) => {
    const canonical = { recent_conversation: rows, transcript_sequence: sequence };
    return { envelope: canonical, canonicalText: JSON.stringify(canonical) } as never;
  };

  it('delivers an API-sized claimed message in full after verifying its bounded envelope row', async () => {
    const tail = 'TASK-2-TAIL';
    const content = `${'x'.repeat(20_000 - tail.length)}${tail}`;
    const message = { id: 'long-message', content, from_type: 'user' };

    const prompt = await buildTypedFastDispatchPrompt(
      verified([row(message.id, 41, content)], 41),
      [message],
      0,
    );

    expect(JSON.stringify(prompt)).toContain(tail);
    expect(JSON.stringify(prompt)).toContain(content);
  });

  it('rejects altered claimed content', () => {
    const original = { id: 'message-1', content: 'exact original', from_type: 'user' };
    expect(() => assertSelectedMessagesMatchEnvelope(
      verified([row(original.id, 1, original.content)], 1),
      [{ ...original, content: 'altered original' }],
    )).toThrow('content does not match');
  });

  it('rejects selected messages whose order differs from the signed sequence', () => {
    const first = { id: 'message-1', content: 'first', from_type: 'user' };
    const second = { id: 'message-2', content: 'second', from_type: 'user' };
    expect(() => assertSelectedMessagesMatchEnvelope(
      verified([row(first.id, 1, first.content), row(second.id, 2, second.content)], 2),
      [second, first],
    )).toThrow('order does not match');
  });

  it('rejects selected messages whose identity is absent from the envelope', () => {
    const content = 'same bytes are not the same message';
    expect(() => assertSelectedMessagesMatchEnvelope(
      verified([row('canonical-id', 1, content)], 1),
      [{ id: 'different-id', content, from_type: 'user' }],
    )).toThrow('not bound exactly once');
  });

  it('rejects empty input and duplicate canonical bindings', () => {
    expect(() => assertSelectedMessagesMatchEnvelope(verified([], 0), []))
      .toThrow('at least one message');
    const message = { id: 'duplicate', content: 'exact', from_type: 'user' };
    const bound = row(message.id, 1, message.content);
    expect(() => assertSelectedMessagesMatchEnvelope(
      verified([bound, { ...bound }], 1),
      [message],
    )).toThrow('not bound exactly once');
    expect(() => assertSelectedMessagesMatchEnvelope(
      verified([{ ...bound, from_type: 'agent' }], 1),
      [message],
    )).toThrow('canonical user row');
  });

  it('rejects a correctly hashed earlier row that is not the terminal turn', () => {
    const message = { id: 'earlier', content: 'old input', from_type: 'user' };
    expect(() => assertSelectedMessagesMatchEnvelope(
      verified([
        row(message.id, 40, message.content),
        row('terminal', 41, 'new input'),
      ], 41),
      [message],
    )).toThrow('not the terminal');
  });

  it('uses UTF-8 byte counts rather than character counts', () => {
    const message = { id: 'unicode', content: 'task 🚀 café', from_type: 'user' };
    expect(() => assertSelectedMessagesMatchEnvelope(
      verified([row(message.id, 5, message.content)], 5),
      [message],
    )).not.toThrow();
    expect(() => assertSelectedMessagesMatchEnvelope(
      verified([{ ...row(message.id, 5, message.content), content_bytes: message.content.length }], 5),
      [message],
    )).toThrow('content does not match');
  });

  it('binds stable attachment identity while excluding expiring URL signatures', () => {
    const attachment = {
      attachment_id: '11111111-1111-4111-8111-111111111111',
      type: 'image',
      mime_type: 'image/png',
      name: 'diagram.png',
      size_bytes: 42,
      checksum: 'a'.repeat(64),
      url: 'https://storage.googleapis.com/private-bucket/diagram.png?X-Goog-Signature=first',
    };
    const message = {
      id: 'with-attachment', content: 'inspect', from_type: 'user',
      context: { attachments: [attachment] },
    };
    const bound = {
      ...row(message.id, 9, message.content),
      attachment_manifest: canonicalAttachmentManifest([{ ...attachment, url: `${attachment.url}2` }]),
    };
    expect(canonicalAttachmentManifest([{
      ...attachment,
      url: attachment.url.replace('/private-bucket/', '/PRIVATE-BUCKET/'),
    }])).toEqual(canonicalAttachmentManifest([attachment]));
    expect(canonicalAttachmentManifest([attachment])[0]?.locator_sha256).toBe(
      createHash('sha256').update('gs://private-bucket/diagram.png').digest('hex'),
    );
    expect(canonicalAttachmentManifest([attachment])[0]?.descriptor_sha256).toBe(
      '78521219af06448fd59b3516b671b66aecb691308562f5096b90412c8e10cf80',
    );
    expect(() => assertSelectedMessagesMatchEnvelope(verified([bound], 9), [message]))
      .not.toThrow();
    expect(() => assertSelectedMessagesMatchEnvelope(verified([bound], 9), [{
      ...message,
      context: { attachments: [{
        ...attachment,
        url: 'https://storage.googleapis.com/private-bucket/other.png?X-Goog-Signature=first',
      }] },
    }])).toThrow('attachments do not match');
    expect(() => canonicalAttachmentManifest([{
      ...attachment,
      size_bytes: Number.MAX_SAFE_INTEGER + 1,
    }])).toThrow('size is invalid');
    expect(() => canonicalAttachmentManifest([{
      ...attachment,
      original_char_count: Number.MAX_SAFE_INTEGER + 1,
    }])).toThrow('original_char_count is invalid');
  });

  it.each([
    ['restart', 0, 'short active text'],
    ['normal', 40, 'short active text'],
    ['restart-long', 0, `${'x'.repeat(8_000)}TAIL`],
    ['normal-long', 40, `${'y'.repeat(8_000)}TAIL`],
  ])('renders the full active turn exactly once on %s input', async (_name, cursor, content) => {
    const message = { id: `message-${cursor}-${content.length}`, content, from_type: 'user' };
    const prompt = await buildTypedFastDispatchPrompt(
      verified([row(message.id, 41, content)], 41),
      [message],
      cursor,
    );
    expect(JSON.stringify(prompt).split(content)).toHaveLength(2);
  });

  it('keeps terminal operator constraints authoritative before a Workday consultation', async () => {
    const content = [
      'Read the canonical problem model and running update through one private Workday consultation.',
      'No operator decision is needed. Do not propose alternatives, retries, or an immutable-version rewrite.',
      'Carry the settled abandon-before-qualification conclusion forward unchanged.',
    ].join(' ');
    const message = { id: 'terminal-consultation-request', content, from_type: 'user' };

    const prompt = await buildTypedFastDispatchPrompt(
      verified([row(message.id, 42, content)], 42),
      [message],
      0,
    );
    const rendered = JSON.stringify(prompt);

    expect(rendered).toContain('## Operator instruction fidelity');
    expect(rendered).toContain('The active operator turn is authoritative for scope, negative constraints, decision ownership, and the requested stopping point.');
    expect(rendered).toContain('No operator decision is needed. Do not propose alternatives');
    expect(rendered).toContain('Requesting a private Workday consultation does not relax these constraints.');
    expect(rendered).toContain('Never turn a settled terminal instruction into a menu of ways around it');
    expect(rendered.split(content)).toHaveLength(2);
  });
});

describe('cold-start Fast lane — live strategic_session stream', () => {
  it('abandons a nonterminal signed input without dispatching it', async () => {
    const nonterminalEnvelope = {
      ...envelope,
      transcript_sequence: 8,
      recent_conversation: [
        ...envelope.recent_conversation,
        {
          sequence: 8,
          message_id: 'newer-message',
          from_type: 'user',
          content: 'newer',
          content_bytes: 5,
          content_sha256: createHash('sha256').update('newer').digest('hex'),
          attachment_manifest: [],
        },
      ],
    };
    mocks.apiPost.mockImplementation((url: string) => {
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({ acquired: true, lease: { lease_id: 'lease-1', generation: 1 } });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        return Promise.resolve({
          claim_id: 'claim-nonterminal', owner: 'fast', message_ids: [MESSAGE_ID],
          envelope: nonterminalEnvelope,
        });
      }
      if (url.endsWith('/conversation/turns/abandon')) {
        return Promise.resolve({ status: 'abandoned', legacy_fallback_admissible: true });
      }
      return Promise.resolve({});
    });

    await runCheckoutIndependentFastStartup(startOptions({}));

    expect(mocks.dispatch).not.toHaveBeenCalled();
    expect(mocks.apiPost.mock.calls.filter(([url]) =>
      String(url).endsWith('/conversation/turns/abandon'))).toHaveLength(1);
  });

  it('coalesces typed deltas into bounded conversation-message chunks', async () => {
    vi.useFakeTimers();
    const streamer = new ConversationMessageStreamer(
      {} as never, 'session-1', 'task-1', 'claim-1', 'typed-fast:reply-1',
    );
    streamer.delta('A');
    streamer.delta('nswer');
    await vi.advanceTimersByTimeAsync(100);
    streamer.delta('!');
    const finished = streamer.finish();
    await vi.advanceTimersByTimeAsync(300);
    await finished;

    const frames = (mocks.sendWsFrame.mock.calls as unknown as Array<[unknown, unknown]>)
      .map(([, frame]) => frame as { type: string; payload: Record<string, unknown> });
    expect(frames.filter(frame => frame.type === 'conversation_message_delta').map(frame => frame.payload))
      .toEqual([
        {
          session_id: 'session-1', task_id: 'task-1', claim_id: 'claim-1',
          idempotency_key: 'typed-fast:reply-1', attempt: 0, seq: 0, content: 'Answer',
        },
        {
          session_id: 'session-1', task_id: 'task-1', claim_id: 'claim-1',
          idempotency_key: 'typed-fast:reply-1', attempt: 0, seq: 1, content: '!',
        },
      ]);
  });

  it('splits a long provider response before it reaches the websocket transport', async () => {
    vi.useFakeTimers();
    const streamer = new ConversationMessageStreamer(
      {} as never, 'session-1', 'task-1', 'claim-1', 'typed-fast:long-reply',
    );
    const reply = 'x'.repeat(STREAM_DELTA_MAX_CHARS + 17);
    streamer.delta(reply);
    const finished = streamer.finish();
    await vi.advanceTimersByTimeAsync(900);
    await finished;

    const payloads = (mocks.sendWsFrame.mock.calls as unknown as Array<[unknown, unknown]>)
      .map(([, frame]) => frame as { type: string; payload: { content: string } })
      .filter(frame => frame.type === 'conversation_message_delta')
      .map(frame => frame.payload);
    // The raw slice is deliberately BELOW the wire cap so redaction has
    // headroom. 1_200 is the literal the backend relay drops chunks above
    // (_STREAMING_PREVIEW_MAX_CHUNK_CHARS) -- pinned here, not imported.
    expect(payloads.map(payload => payload.content.length)).toEqual([1_000, 217]);
    for (const payload of payloads) expect(payload.content.length).toBeLessThanOrEqual(1_200);
    // Nothing is lost by the split.
    expect(payloads.map(payload => payload.content).join('')).toBe(reply);
  });

  it('emits a failed terminal tombstone for an abandoned preview', async () => {
    const streamer = new ConversationMessageStreamer(
      {} as never, 'session-1', 'task-1', 'claim-1', 'typed-fast:failed-reply',
    );
    streamer.delta('partial');
    await streamer.finish('abandoned');

    const frames = (mocks.sendWsFrame.mock.calls as unknown as Array<[unknown, unknown]>)
      .map(([, frame]) => frame as { type: string; payload: Record<string, unknown> })
      .filter(frame => frame.type === 'conversation_message_delta');
    expect(frames.at(-1)?.payload).toMatchObject({ content: '', terminal: 'abandoned' });
  });

  it('still emits the tombstone when finish() already ran without a terminal', async () => {
    // The success path calls finish() with no terminal, then the completion
    // POST throws. Without a late tombstone the operator keeps a
    // complete-looking answer that does not exist server-side.
    const streamer = new ConversationMessageStreamer(
      {} as never, 'session-1', 'task-1', 'claim-1', 'typed-fast:late-terminal',
    );
    streamer.delta('partial');
    await streamer.finish();
    await streamer.finish('failed');

    const frames = (mocks.sendWsFrame.mock.calls as unknown as Array<[unknown, unknown]>)
      .map(([, frame]) => frame as { type: string; payload: Record<string, unknown> })
      .filter(frame => frame.type === 'conversation_message_delta');
    expect(frames.filter(frame => frame.payload.terminal !== undefined)).toHaveLength(1);
    expect(frames.at(-1)?.payload).toMatchObject({ content: '', terminal: 'failed' });
  });

  it('emits responding, incremental deltas and a terminating done for a normal turn', async () => {
    mocks.dispatch.mockImplementation(() => {
      emit({ kind: 'text', text: 'Looking' });
      emit({ kind: 'tool', toolName: 'Bash', text: 'ls -1' });
      emit({ kind: 'activity', activity: { kind: 'action', text: 'Read foo.ts' } });
      emit({ kind: 'text', text: ' at it.' });
      return Promise.resolve({
        receipt: { claim_id: 'claim-1', message_ids: [MESSAGE_ID], idempotency_key: 'k' },
        result: turnResult(),
        completion: { receipt: {}, content: 'Here is the answer.' },
        workdayRequests: [],
      });
    });

    await runCheckoutIndependentFastStartup(startOptions({}));
    await waitForDone();

    const frames = sessionFrames();
    // Lifecycle opens the turn before any content reaches the operator.
    expect(frames[0].status).toBe('responding');
    expect(frames[frames.length - 1].status).toBe('done');
    // seq is monotonic so the UI can order/dedupe.
    expect(frames.map(f => f.seq)).toEqual(frames.map((_, i) => i));
    // Assistant text streams as `message` on the PROTECTED /strategic-session
    // live transcript; the two deltas coalesce in order. This must keep holding
    // now that the conversation preview exists: the preview MIRRORS the text,
    // it does not divert it off the transcript.
    expect(frames.filter(f => f.kind === 'message').map(f => f.text).join(''))
      .toBe('Looking at it.');
    // ...and the same text also streams as ordered conversation chunks, which
    // the UI appends without a canonical refetch for an ephemeral preview.
    expect(conversationFrames().map(f => f.content)).toEqual(['Looking at it.']);
    expect(conversationFrames().map(f => f.seq)).toEqual([0]);
    expect(frames.some(f => f.kind === 'tool' && f.text === '[tool: Bash] ls -1')).toBe(true);
    expect(frames.some(f => f.kind === 'action' && f.text === 'Read foo.ts')).toBe(true);
    // Exactly one durable commit through the unchanged completion protocol.
    expect(completeCalls()).toHaveLength(1);
    expect((completeCalls()[0][1] as { content: string }).content).toBe('Here is the answer.');
  });

  it('terminates the turn with done when the provider turn fails', async () => {
    mocks.dispatch.mockImplementation(() => {
      emit({ kind: 'text', text: 'partial' });
      return Promise.reject(new Error('provider exploded'));
    });

    await runCheckoutIndependentFastStartup(startOptions({}));
    await waitForDone();

    const frames = sessionFrames();
    expect(frames[0].status).toBe('responding');
    // Exactly one terminating frame — no consumer is left with a stuck turn.
    expect(frames.filter(f => f.status === 'done')).toHaveLength(1);
    expect(frames[frames.length - 1].status).toBe('done');
    // Observational only: a failed turn still commits nothing.
    expect(completeCalls()).toHaveLength(0);
  });

  it('terminates the turn with done when the turn is interrupted mid-dispatch', async () => {
    mocks.dispatch.mockImplementation(() => {
      emit({ kind: 'text', text: 'partial' });
      return Promise.resolve({
        receipt: { claim_id: 'claim-1', message_ids: [MESSAGE_ID], idempotency_key: 'k' },
        result: turnResult({ interrupted: true, processGone: true }),
        completion: null,
        workdayRequests: [],
      });
    });

    await runCheckoutIndependentFastStartup(startOptions({}));
    await waitForDone();

    const frames = sessionFrames();
    expect(frames.filter(f => f.status === 'responding')).toHaveLength(1);
    expect(frames.filter(f => f.status === 'done')).toHaveLength(1);
    expect(frames[frames.length - 1].status).toBe('done');
    expect(completeCalls()).toHaveLength(0);
  });

  it('falls back to the buffered log with no viewer socket and still commits once', async () => {
    // No live viewer socket: every frame fails to send and must fall back.
    mocks.sendWsFrame.mockReturnValue(false);
    mocks.dispatch.mockImplementation(() => {
      emit({ kind: 'text', text: 'streamed anyway' });
      return Promise.resolve({
        receipt: { claim_id: 'claim-1', message_ids: [MESSAGE_ID], idempotency_key: 'k' },
        result: turnResult(),
        completion: { receipt: {}, content: 'Durable reply.' },
        workdayRequests: [],
      });
    });

    await runCheckoutIndependentFastStartup(startOptions(null));
    await vi.waitFor(() => {
      expect(mocks.sendLog.mock.calls.some(([, line]) => String(line).includes('streamed anyway')))
        .toBe(true);
    }, { timeout: 5_000, interval: 20 });

    // The durable reply is unaffected by the absent stream.
    expect(completeCalls()).toHaveLength(1);
    expect((completeCalls()[0][1] as { content: string }).content).toBe('Durable reply.');
  });
});
