import { beforeEach, describe, expect, it, vi } from 'vitest';
import { sanitize as realSanitize } from '../utils/sanitize.js';
import { ConversationMessageStreamer } from './conversation-message-streamer.js';
import { SessionStreamer } from './session-streamer.js';
import { WsFrameBudget } from './ws-frame-budget.js';

const mocks = vi.hoisted(() => ({
  sendLog: vi.fn(),
  /** Virtual-clock timestamp of every frame, so a test can slide a one-second
   *  window over the socket and assert the aggregate rate ceiling. */
  frames: [] as Array<{ at: number; type: string; payload: Record<string, unknown> }>,
  sendWsFrame: vi.fn(),
}));

// The REAL `sanitize` is deliberately wired in here. The sibling
// `strategic-resident-cold-fast.stream.test.ts` stubs it to identity — correct
// for testing lane wiring, useless for testing redaction. The defect this file
// pins was precisely a missing `sanitize()` call on an operator-facing channel,
// and an identity stub cannot fail on it.
vi.mock('../index.js', () => ({
  sanitize: realSanitize,
  sendLog: mocks.sendLog,
  sendWsFrame: mocks.sendWsFrame,
}));

const conversationFrames = () => mocks.frames.filter(f => f.type === 'conversation_message_delta');
const contents = () => conversationFrames().map(f => String(f.payload.content));

beforeEach(() => {
  mocks.frames.length = 0;
  mocks.sendLog.mockReset();
  mocks.sendWsFrame.mockReset();
  mocks.sendWsFrame.mockImplementation((_ws: unknown, frame: unknown) => {
    const typed = frame as { type: string; payload: Record<string, unknown> };
    mocks.frames.push({ at: Date.now(), type: typed.type, payload: typed.payload });
    return true;
  });
});

const newStreamer = (key: string, budget?: WsFrameBudget) =>
  new ConversationMessageStreamer({} as never, 'session-1', 'task-1', 'claim-1', key, budget);

describe('ConversationMessageStreamer — secret redaction', () => {
  it('redacts credentials before the chunk reaches the websocket', async () => {
    const streamer = newStreamer('typed-fast:secret');
    streamer.delta('Use ghp_abcdefghijklmnopqrstuvwxyz012345 to push, and ');
    streamer.delta('send Authorization: Bearer eyJhbGciOi.eyJzdWIi.sig next.');
    await streamer.finish();

    const sent = contents().join('');
    // `sendWsFrame` does NOT sanitize (unlike `sendLog`), so a raw provider
    // chunk relayed straight to the operator socket leaks verbatim.
    expect(sent).not.toContain('ghp_abcdefghijklmnopqrstuvwxyz012345');
    expect(sent).not.toContain('eyJhbGciOi.eyJzdWIi.sig');
    // Masked, not merely dropped — the operator still sees a coherent reply.
    expect(sent).toContain('ghp_***');
    expect(sent).toContain('Bearer ***');
    expect(sent).toContain('to push, and');
  });

  it('redacts the buffered fallback taken when no live socket is attached', async () => {
    mocks.sendWsFrame.mockImplementation(() => false);
    const streamer = newStreamer('typed-fast:secret-fallback');
    streamer.delta('key sk-ant-abcdefghijklmnopqrstuvwxyz0123456789 here');
    await streamer.finish();

    const logged = mocks.sendLog.mock.calls.map(([, text]) => String(text)).join('');
    expect(logged).not.toContain('sk-ant-abcdefghijklmnopqrstuvwxyz0123456789');
    expect(logged).toContain('sk-ant-***');
  });

  it('keeps a redacted chunk under the wire cap even when redaction grows it', async () => {
    vi.useFakeTimers();
    // Pathological case: `ghp_a` (5 chars) redacts to `ghp_***` (7), so a raw
    // 1,000-char slice would sanitize to ~1,330 — over the 1,200 the backend
    // relay DROPS. Dropping is the bad outcome: the chunk carrying the secret
    // is exactly the one that must arrive, redacted.
    const streamer = newStreamer('typed-fast:growth');
    streamer.delta('ghp_a\n'.repeat(400));
    const finished = streamer.finish();
    await vi.advanceTimersByTimeAsync(2_000);
    await finished;
    vi.useRealTimers();

    expect(conversationFrames().length).toBeGreaterThan(1);
    for (const chunk of contents()) {
      expect(chunk.length).toBeLessThanOrEqual(1_200);
      // The shrink loop must not degenerate to one-char chunks either.
      expect(chunk.length).toBeGreaterThan(100);
    }
    expect(contents().join('')).not.toContain('ghp_a');
    expect(contents().join('')).toContain('ghp_***');
  });
});

describe('ConversationMessageStreamer — shared socket frame budget', () => {
  it('holds the whole socket under 20 frames/second with two concurrent streamers', async () => {
    vi.useFakeTimers();
    // ONE budget, exactly as the resident lanes wire it. The backend agent-WS
    // receiver closes the socket (1008 "Rate limit exceeded") above 20 inbound
    // messages/second counted across EVERY frame type, so a per-streamer
    // "I own half of 20" budget breaks the moment a second streamer appears.
    const budget = new WsFrameBudget();
    const transcript = new SessionStreamer({} as never, 'session-1', 'task-1', budget);
    const streamer = newStreamer('typed-fast:huge', budget);

    // ~30,000 chars of reply. Under the old unbounded synchronous drain this
    // emitted its whole residual in ONE tick at finish(), tripping the close
    // and taking the running task's log and status relay down with it.
    streamer.delta('y'.repeat(30_000));
    transcript.responding();
    transcript.delta('mirrored transcript text');
    for (let i = 0; i < 12; i += 1) transcript.tool('Bash', `step-${i}`);

    const finished = streamer.finish('failed');
    await vi.advanceTimersByTimeAsync(10_000);
    await finished;
    await vi.advanceTimersByTimeAsync(10_000);
    vi.useRealTimers();

    // Both streamers really did write to the socket.
    expect(conversationFrames().length).toBeGreaterThan(3);
    expect(mocks.frames.filter(f => f.type === 'strategic_session').length).toBeGreaterThan(3);
    // Sliding one-second window over every frame on the shared socket.
    for (let i = 0; i < mocks.frames.length; i += 1) {
      const from = mocks.frames[i].at;
      const inWindow = mocks.frames.filter(f => f.at >= from && f.at < from + 1_000);
      expect(inWindow.length).toBeLessThanOrEqual(20);
    }
  });

  it('bounds the finish() drain instead of flushing the whole buffer at once', async () => {
    vi.useFakeTimers();
    const streamer = newStreamer('typed-fast:bounded');
    streamer.delta('z'.repeat(60_000));
    const startedAt = Date.now();
    const finished = streamer.finish('failed');
    await vi.advanceTimersByTimeAsync(20_000);
    await finished;
    vi.useRealTimers();

    // 60,000 chars is ~60 chunks; draining them all at <=10/s would hold the
    // turn for ~6s. The bounded window stops at ~900ms, discards the residual
    // (the durable receipt-bound reply carries the full text) and still sends
    // the tombstone so the client never keeps a half answer on screen.
    // Measured on the FRAMES, not on Date.now() after the timer advance.
    const lastFrameAt = conversationFrames().at(-1)?.at ?? Infinity;
    expect(lastFrameAt - startedAt).toBeLessThan(2_000);
    expect(conversationFrames().length).toBeLessThan(20);
    expect(conversationFrames().at(-1)?.payload).toMatchObject({ content: '', terminal: 'failed' });
  });
});

describe('ConversationMessageStreamer — retry identity', () => {
  it('gives a retried attempt a fresh identity and tombstones the abandoned one', async () => {
    vi.useFakeTimers();
    const streamer = newStreamer('typed-fast:retry');
    streamer.delta('Half an ans');
    await vi.advanceTimersByTimeAsync(200);

    // A provider crash plus `replaceAfterCrash()` re-runs the SAME receipt from
    // the top, so attempt 2 is a new answer rather than a continuation. Reusing
    // one preview identity made the client weld attempt 2's tail onto attempt
    // 1's partial (its `seq <= current` guard swallows the restarted prefix),
    // rendering a coherent reply the model never wrote.
    streamer.newAttempt();
    streamer.delta('A complete answer.');
    const finished = streamer.finish();
    await vi.advanceTimersByTimeAsync(1_000);
    await finished;
    vi.useRealTimers();

    const frames = conversationFrames();
    expect(frames.filter(f => f.payload.attempt === 0).map(f => f.payload.content))
      .toEqual(['Half an ans', '']);
    expect(frames.find(f => f.payload.terminal !== undefined)?.payload)
      .toMatchObject({ attempt: 0, terminal: 'failed' });
    // A DISTINCT attempt restarting at seq 0 — never appended to attempt 0.
    expect(frames.filter(f => f.payload.attempt === 1).map(f => f.payload.content))
      .toEqual(['A complete answer.']);
    expect(frames.filter(f => f.payload.attempt === 1).map(f => f.payload.seq)).toEqual([0]);
  });
});
