import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ResidentSessionEvent } from '../providers/resident-session.js';

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
  buildFastResidentContextFrame: () => 'CONTEXT',
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

import { runCheckoutIndependentFastStartup } from './strategic-resident-cold-fast.js';

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

describe('cold-start Fast lane — live strategic_session stream', () => {
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
    // Assistant text streams as `message`; the two deltas coalesce in order.
    expect(frames.filter(f => f.kind === 'message').map(f => f.text).join(''))
      .toBe('Looking at it.');
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
