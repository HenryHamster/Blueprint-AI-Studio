import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

// ── Hoisted mock sinks ────────────────────────────────────────────────────────
const { mockSendWsFrame, mockSendLog } = vi.hoisted(() => ({
  mockSendWsFrame: vi.fn(),
  mockSendLog: vi.fn(),
}));

// ── Module mocks ──────────────────────────────────────────────────────────────
// Only SessionStreamer is under test; stub the handler's heavy dependencies so
// importing strategic-resident.ts stays light. strategic-resident-lib.js (which
// owns the pure FrameCoalescer) is left REAL so the queue policy is exercised.
vi.mock('../index.js', () => ({
  WORKSPACE: '',
  strVal: vi.fn((v: unknown) => (typeof v === 'string' ? v : '')),
  sanitize: vi.fn((s: string) => s),
  sendLog: mockSendLog,
  sendWsFrame: mockSendWsFrame,
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  persistAndPostStatus: vi.fn(),
  resolveBaseBranch: vi.fn(() => 'main'),
}));
vi.mock('../workspacePrep.js', () => ({ prepareWorkspaceForRole: vi.fn() }));
vi.mock('../prompt.js', () => ({
  buildContextPrefix: vi.fn(() => ''),
  configureAgentPermissions: vi.fn(() => []),
  configureResidentAgentPermissions: vi.fn(() => []),
  writePlaybookHelper: vi.fn(),
  writeMessagesHelper: vi.fn(),
  SHARED_LIFECYCLE_REMAINDER: '',
}));
vi.mock('./strategic-helpers.js', () => ({ writeStrategicHelpers: vi.fn() }));
vi.mock('../providers/runners.js', () => ({
  provisionAmbientHelpers: vi.fn(),
}));
vi.mock('../providers/resident-session.js', () => ({
  startResidentClaudeSession: vi.fn(),
}));
vi.mock('./strategic-sections.js', () => ({
  buildDirSummaryText: vi.fn(() => ''),
  buildOpenMessagesText: vi.fn(() => ''),
  buildDirectionPortfolioSection: vi.fn(() => ''),
  buildPendingFollowupsSection: vi.fn(() => ''),
}));

// ── Import after mocks ────────────────────────────────────────────────────────
import { SessionStreamer } from './strategic-resident.js';

// The coalescing drain emits at most one frame per this window (see
// STREAM_FLUSH_MS in strategic-resident.ts); mirrored here for the timing math.
const STREAM_FLUSH_MS = 100;

interface SessionFrame { payload: { kind: string; text: string } }
const emittedTexts = (): string[] =>
  mockSendWsFrame.mock.calls.map((c) => (c[1] as SessionFrame).payload.text);

describe('SessionStreamer.flush — rate-limited boundary flush', () => {
  beforeEach(() => {
    mockSendWsFrame.mockReset();
    mockSendLog.mockReset();
    mockSendWsFrame.mockReturnValue(true); // simulate a live viewer socket
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it('hands a large residual to the rate-limited drain instead of bursting it synchronously', async () => {
    const streamer = new SessionStreamer(null, 'sess', 'task');
    // A burst of 25 tool frames queued in one assistant message right before the
    // turn-end flush — the exact shape that could trip the backend 20 msg/s cap.
    for (let i = 0; i < 25; i++) streamer.tool('Bash', `cmd-${i}`);

    streamer.flush();

    // Synchronously, flush must NOT dump the whole queue (that would exceed the
    // backend's 20-frame/s WS-close threshold). At most one frame has gone out.
    expect(mockSendWsFrame.mock.calls.length).toBeLessThanOrEqual(1);

    // The residual then trickles out at <=10/s (one per STREAM_FLUSH_MS).
    await vi.advanceTimersByTimeAsync(STREAM_FLUSH_MS * (25 + 5));

    // Every frame delivered, none lost, in original order.
    expect(mockSendWsFrame.mock.calls.length).toBe(25);
    expect(emittedTexts()).toEqual(
      Array.from({ length: 25 }, (_, i) => `[tool: Bash] cmd-${i}`),
    );
  });

  it('spaces the flushed frames across rate windows instead of bursting them', async () => {
    const streamer = new SessionStreamer(null, 'sess', 'task');
    const N = 10;
    for (let i = 0; i < N; i++) streamer.tool('Bash', `c${i}`);
    streamer.flush();

    // Advance window-by-window; cumulative delivery can never run ahead of the
    // rate budget (one per window) by more than the single leading-edge frame.
    // In particular it can NEVER reach N in the first few windows — a burst is
    // structurally impossible.
    for (let windows = 1; windows <= N + 2; windows++) {
      await vi.advanceTimersByTimeAsync(STREAM_FLUSH_MS);
      expect(mockSendWsFrame.mock.calls.length).toBeLessThanOrEqual(windows + 1);
    }
    expect(mockSendWsFrame.mock.calls.length).toBe(N);
  });

  it('seals the ending turn’s open text so the next turn’s delta starts a fresh frame', async () => {
    const streamer = new SessionStreamer(null, 'sess', 'task');
    streamer.delta('end of turn A');
    streamer.flush(); // boundary: seal A's tail
    streamer.delta('start of turn B');

    await vi.advanceTimersByTimeAsync(STREAM_FLUSH_MS * 5);

    // Two SEPARATE message frames — A's tail never merges into B's first delta.
    expect(emittedTexts()).toEqual(['end of turn A', 'start of turn B']);
  });
});
