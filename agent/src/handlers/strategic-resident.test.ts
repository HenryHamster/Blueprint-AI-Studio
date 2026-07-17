import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import fs from 'fs';

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
  normalizeClaudeConfigContent: vi.fn((_path: string, content: string) => content),
  WORKSPACE: '',
  strVal: vi.fn((v: unknown) => (typeof v === 'string' ? v : '')),
  sanitize: vi.fn((s: string) => s),
  sendLog: mockSendLog,
  sendWsFrame: mockSendWsFrame,
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  persistAndPostStatus: vi.fn(),
  resolveBaseBranch: vi.fn(() => 'main'),
  recordResidentTurnLatency: vi.fn(),
  getResidentTurnLatencyTelemetry: vi.fn(() => null),
  recordOperatorDecisionCapture: vi.fn(),
  getOperatorDecisionCaptureTelemetry: vi.fn(() => null),
}));
vi.mock('../workspacePrep.js', () => ({
  prepareWorkspaceForRole: vi.fn(),
  listWorkspaceTopDirs: vi.fn(() => []),
}));
vi.mock('../git-credential.js', () => ({
  writeGitTokenFile: vi.fn(),
  setupGitCredentialHelper: vi.fn(),
  ensureGitCredentialHelperScript: vi.fn(() => ''),
  gitTokenFilePath: vi.fn(() => ''),
  gitCredentialHelperPath: vi.fn(() => ''),
  GIT_TOKEN_REFRESH_MS: 2_400_000,
  GH_READ_TOKEN_FILE_ENV: 'GH_READ_TOKEN_FILE',
  GH_CREDENTIAL_HELPER_PATH_ENV: 'GH_CREDENTIAL_HELPER_PATH',
}));
vi.mock('../prompt.js', () => ({
  buildContextPrefix: vi.fn(() => ''),
  configureAgentPermissions: vi.fn(() => []),
  configureResidentAgentPermissions: vi.fn(() => []),
  writePlaybookHelper: vi.fn(),
  writeMessagesHelper: vi.fn(),
  writeSkillHelpers: vi.fn(),
  formatSkills: vi.fn(),
  formatProjectTools: vi.fn(),
  SHARED_LIFECYCLE_REMAINDER: '',
}));
vi.mock('./strategic-helpers.js', () => ({
  writeStrategicHelpers: vi.fn(),
  buildFetchGuidance: vi.fn(() => ''),
  STRATEGIC_FETCH_VERBS: [],
}));
vi.mock('../providers/runners.js', () => ({
  provisionAmbientHelpers: vi.fn(),
  claudeHelpSupportsPartialMessages: vi.fn(() => true),
  __resetClaudeCapabilityCacheForTesting: vi.fn(),
  probeClaudeCapabilities: vi.fn(() => null),
  stripUnsupportedClaudeOptionalArgs: vi.fn((args: readonly string[]) => [...args]),
}));
vi.mock('../providers/resident-session.js', () => ({
  startResidentClaudeSession: vi.fn(),
}));
vi.mock('./strategic-sections.js', () => ({
  buildDirSummaryText: vi.fn(() => ''),
  // Render each open message so the compaction checkpoint's "Open operator
  // messages" section reflects exactly which messages the backend still
  // considers open at checkpoint-fetch time — the seam the ordering fix guards.
  buildOpenMessagesText: vi.fn((msgs: Array<{ content?: string }> | undefined) =>
    Array.isArray(msgs) && msgs.length > 0
      ? msgs.map((m) => `MSG:${m.content ?? ''}`).join('\n')
      : ''),
  // Render each recently-answered message so the checkpoint / bootstrap
  // read-only tail reflects exactly the handled conversation the backend
  // surfaces at fetch time.
  formatRecentHandledConversation: vi.fn((msgs: Array<{ content?: string }> | undefined) =>
    Array.isArray(msgs) && msgs.length > 0
      ? msgs.map((m) => `HANDLED:${m.content ?? ''}`).join('\n')
      : ''),
  buildDirectionPortfolioSection: vi.fn(() => ''),
  buildPendingFollowupsSection: vi.fn(() => ''),
}));

// ── Import after mocks ────────────────────────────────────────────────────────
import {
  SessionStreamer, handleStrategicResident, refreshResidentGitReadToken,
  recloneResidentWorkspace,
} from './strategic-resident.js';
import {
  apiGet, apiPost, recordOperatorDecisionCapture, recordResidentTurnLatency,
} from '../index.js';
import { writeGitTokenFile, setupGitCredentialHelper } from '../git-credential.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import { writePlaybookHelper, buildContextPrefix } from '../prompt.js';
import { writeStrategicHelpers } from './strategic-helpers.js';
import { provisionAmbientHelpers } from '../providers/runners.js';
import { startResidentClaudeSession } from '../providers/resident-session.js';
import type {
  ResidentSession, ResidentSessionEvent, ResidentTurnResult,
} from '../providers/resident-session.js';

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

  it('interleaves tool frames between adjacent text deltas in order', async () => {
    const streamer = new SessionStreamer(null, 'sess', 'task');
    streamer.delta('checking ');
    streamer.tool('Read', '/some/file');
    streamer.delta(' result found');

    streamer.flush();
    await vi.advanceTimersByTimeAsync(STREAM_FLUSH_MS * 10);

    // The tool frame seals any preceding open text and sits between deltas.
    expect(emittedTexts()).toEqual([
      'checking ',
      '[tool: Read] /some/file',
      ' result found',
    ]);
  });
});

// ── Operator-turn compaction ordering ─────────────────────────────────────────
// The fix reorders the operator epilogue so the durable decision is registered
// and the answered message is marked handled (via the heartbeat) BEFORE the
// context-compaction checkpoint is built and primed into the fresh subprocess.
// Before the fix, maybeCompact ran first, so the primed session omitted the
// just-settled decision and re-surfaced the just-answered message as still-open.

const mockApiGet = vi.mocked(apiGet);
const mockApiPost = vi.mocked(apiPost);
const mockStartSession = vi.mocked(startResidentClaudeSession);
const mockWriteGitTokenFile = vi.mocked(writeGitTokenFile);
const mockSetupGitCredentialHelper = vi.mocked(setupGitCredentialHelper);
const mockPrepareWorkspaceForRole = vi.mocked(prepareWorkspaceForRole);
const mockWritePlaybookHelper = vi.mocked(writePlaybookHelper);
const mockWriteStrategicHelpers = vi.mocked(writeStrategicHelpers);
const mockProvisionAmbientHelpers = vi.mocked(provisionAmbientHelpers);
const mockBuildContextPrefix = vi.mocked(buildContextPrefix);

/** A ResidentTurnResult with sane defaults, overridable per test. */
function turnResult(over: Partial<ResidentTurnResult> = {}): ResidentTurnResult {
  return {
    resultText: '',
    accumulatedTurnText: '',
    interrupted: false,
    timedOut: false,
    hardTimedOut: false,
    appliedStrategicOp: false,
    sawToolUse: true,
    sentOperatorMessage: true, // skip the persistReply backstop
    processGone: false,
    ttftMs: 1,
    turnMs: 1,
    toolCallCount: 0,
    totalToolCallDurationMs: 0,
    meanInterTokenMs: null,
    inputTokens: 0,
    ...over,
  };
}

interface MockSession extends ResidentSession {
  _emit: (ev: ResidentSessionEvent) => void;
}

/** Build a mock ResidentSession whose sendTurn is fully scripted. */
function makeSession(
  sendTurn: (text: string | Array<Record<string, unknown>>) => Promise<ResidentTurnResult>,
  alive: () => boolean,
): MockSession {
  let cb: ((ev: ResidentSessionEvent) => void) | null = null;
  return {
    sendTurn: vi.fn(sendTurn),
    interrupt: vi.fn(),
    onEvent: vi.fn((fn: (ev: ResidentSessionEvent) => void) => { cb = fn; }),
    touch: vi.fn(),
    waitForIdle: vi.fn(() => Promise.resolve()),
    close: vi.fn(),
    sessionId: vi.fn(() => 'cli-sess'),
    isAlive: vi.fn(alive),
    exitCode: vi.fn(() => 0),
    _emit: (ev) => { if (cb) cb(ev); },
  };
}

describe('handleStrategicResident — operator-turn compaction includes settled decision and drops answered message', () => {
  beforeEach(() => {
    mockApiGet.mockReset();
    mockApiPost.mockReset();
    mockStartSession.mockReset();
    mockSendLog.mockReset();
    mockSendWsFrame.mockReset();
    mockSendWsFrame.mockReturnValue(true);
    // Workspace guard must NOT fire in general-purpose tests (WORKSPACE='' otherwise
    // always looks vanished). Recovery-specific tests override this spy.
    vi.spyOn(fs, 'existsSync').mockReturnValue(true);
  });
  afterEach(() => { vi.restoreAllMocks(); });

  it('primes the compacted session WITH the settled operator decision and WITHOUT the answered message, and marks the message handled before fetching checkpoint state', async () => {
    // The single operator message whose content must NOT survive into the
    // post-compaction checkpoint once it has been answered + marked handled.
    const OPERATOR_MSG = {
      id: 'msg-1',
      content: 'SHOULD_BE_DROPPED_AFTER_HANDLED — which approach?',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    const DECISION_NOTE = 'settled on approach X per operator';

    // Backend-side handled set + a call-order trace shared across the mocks.
    const handledIds: string[] = [];
    const events: string[] = [];
    let capturedCheckpoint = '';
    let messagePollCount = 0;

    mockApiGet.mockImplementation((url: string) => {
      // WORKSPACE is stubbed to '' here, so the per-turn "workspace vanished"
      // guard fires and re-clones — which mints a fresh READ-scoped token.
      if (url.includes('/api/agent/git-read-token')) {
        return Promise.resolve({ token: 'read-scoped-token' });
      }
      if (url.includes('/messages?')) {
        // Deliver the operator message exactly once, then drain.
        if (messagePollCount++ === 0) return Promise.resolve({ messages: [OPERATOR_MSG] });
        return Promise.resolve({ messages: [] });
      }
      if (url.includes('checkpoint-state')) {
        events.push('checkpoint-state');
        // The backend returns only messages still OPEN — i.e. not yet handled.
        return Promise.resolve({
          problem_model: {}, vision: {}, strategic_memory_notes: [], running_update: '',
          open_messages: [OPERATOR_MSG].filter((m) => !handledIds.includes(m.id)),
        });
      }
      return Promise.resolve(null);
    });

    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('heartbeat')) {
        const ids = (body ?? {}).handled_message_ids;
        if (Array.isArray(ids) && ids.length > 0) {
          handledIds.push(...(ids as string[]));
          events.push('heartbeat:handled');
        }
        return Promise.resolve({ authority_held: true });
      }
      return Promise.resolve({});
    });

    // Session 1 runs the operator turn: high input tokens (trips compaction) and
    // an OPERATOR-DECISION marker. Session 2 is the freshly-primed post-compaction
    // subprocess; it captures the checkpoint prompt and is not alive, so the loop
    // exits after priming.
    const session1 = makeSession(
      () => Promise.resolve(turnResult({
        resultText: `Answered the operator.\nOPERATOR-DECISION: ${DECISION_NOTE}`,
        inputTokens: 5_000, // >= the 100-token threshold set below
      })),
      () => true,
    );
    const session2 = makeSession(
      (text) => {
        events.push('prime');
        capturedCheckpoint = typeof text === 'string' ? text : JSON.stringify(text);
        return Promise.resolve(turnResult({ resultText: 'primed', inputTokens: 0 }));
      },
      () => false, // dead ⇒ loop breaks on the next iteration
    );
    let spawnCount = 0;
    mockStartSession.mockImplementation(() => {
      spawnCount += 1;
      if (spawnCount === 1) return session1;
      events.push('spawn2');
      return session2;
    });

    const payload: Record<string, unknown> = {
      project_name: 'Proj',
      // Explicit low compaction threshold so 5k input tokens trips compaction.
      strategic_session_compaction_threshold_tokens: 100,
      open_messages: [],
    };
    const task: Record<string, unknown> = {
      project_metadata: { project_id: 'proj-1' },
      context: {},
    };

    await handleStrategicResident(
      'task-1', payload, task, 'ghtok', 'owner', 'repo', null, { value: '' },
    );

    // (1) The answered message was marked handled (heartbeat) BEFORE checkpoint
    //     state was fetched, and BEFORE the fresh subprocess was primed.
    const iHandled = events.indexOf('heartbeat:handled');
    const iCheckpoint = events.indexOf('checkpoint-state');
    const iPrime = events.indexOf('prime');
    expect(iHandled).toBeGreaterThanOrEqual(0);
    expect(iCheckpoint).toBeGreaterThanOrEqual(0);
    expect(iPrime).toBeGreaterThanOrEqual(0);
    expect(iHandled).toBeLessThan(iCheckpoint);
    expect(iCheckpoint).toBeLessThan(iPrime);

    // (2) The checkpoint that primed the compacted session INCLUDES the settled
    //     operator decision…
    expect(capturedCheckpoint).toContain(DECISION_NOTE);
    expect(capturedCheckpoint).toContain('[operator]');
    // …and DROPS the just-answered message (it is no longer open).
    expect(capturedCheckpoint).not.toContain('SHOULD_BE_DROPPED_AFTER_HANDLED');
  });

  it('answers an operator message that arrives after an autonomous turn BEFORE paying the compaction restart (operator-first)', async () => {
    // Regression guard for the pickup-lag fix. An operator message that lands
    // during an autonomous turn's tail (caught by the post-turn poll) must be
    // answered ON THE EXISTING session FIRST; the compaction-restart cost is
    // deferred until after the reply. Before the fix, the autonomous turn's
    // maybeCompact ran immediately, so a high-token autonomous turn spawned the
    // fresh subprocess BEFORE the operator's reply — the operator waited the whole
    // restart. Here the autonomous turn's input tokens ALSO trip the compaction
    // threshold, so the ONLY thing keeping compaction from front-running the reply
    // is the operator-first ordering under test.
    const OPERATOR_MSG = {
      id: 'msg-op',
      content: 'operator asks a question mid-autonomous-work',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    const events: string[] = [];
    let capturedCheckpoint = '';
    let messagePollCount = 0;
    vi.mocked(recordResidentTurnLatency).mockClear();

    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/api/agent/git-read-token')) {
        return Promise.resolve({ token: 'read-scoped-token' });
      }
      if (url.includes('/messages?')) {
        // Poll #0 = top-of-loop fast path (no message yet); poll #1 = the
        // post-autonomous-turn poll, which delivers the operator message.
        const n = messagePollCount++;
        if (n === 1) return Promise.resolve({ messages: [OPERATOR_MSG] });
        return Promise.resolve({ messages: [] });
      }
      if (url.includes('checkpoint-state')) {
        return Promise.resolve({
          problem_model: {}, vision: {}, strategic_memory_notes: [],
          running_update: '', open_messages: [],
        });
      }
      return Promise.resolve(null);
    });
    mockApiPost.mockResolvedValue({ authority_held: true });

    // session1 runs BOTH the autonomous turn (call #1) and the operator turn
    // (call #2) — proving the operator was answered before compaction spawned a
    // new subprocess. Both turns report high input tokens (trip the 100-token
    // threshold). session2 is the post-compaction subprocess (dead ⇒ loop exits).
    let s1Calls = 0;
    const session1 = makeSession(
      () => {
        s1Calls += 1;
        events.push(s1Calls === 1 ? 'auto' : 'op');
        return Promise.resolve(turnResult({
          resultText: s1Calls === 1 ? 'closing tool summary' : 'answered the operator',
          accumulatedTurnText: s1Calls === 1
            ? 'TICK-VERDICT: ADVANCED — accumulated tick note survives the closing tool call'
            : '',
          inputTokens: 5_000,
        }));
      },
      () => true,
    );
    const session2 = makeSession(
      (text) => {
        events.push('prime');
        capturedCheckpoint = typeof text === 'string' ? text : JSON.stringify(text);
        return Promise.resolve(turnResult({ inputTokens: 0 }));
      },
      () => false,
    );
    let spawnCount = 0;
    mockStartSession.mockImplementation(() => {
      spawnCount += 1;
      if (spawnCount === 1) return session1;
      events.push('spawn2');
      return session2;
    });

    await handleStrategicResident(
      'task-of', {
        project_name: 'Proj',
        strategic_session_compaction_threshold_tokens: 100,
        open_messages: [],
      },
      { project_metadata: { project_id: 'proj-1' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    // The operator turn ran on the ORIGINAL session (session1's 2nd sendTurn), and
    // strictly BEFORE the compaction subprocess was spawned/primed.
    expect(s1Calls).toBe(2);
    const iOp = events.indexOf('op');
    const iSpawn2 = events.indexOf('spawn2');
    const iPrime = events.indexOf('prime');
    expect(iOp).toBeGreaterThanOrEqual(0);
    expect(iSpawn2).toBeGreaterThan(iOp);
    expect(iPrime).toBeGreaterThan(iSpawn2);
    expect(capturedCheckpoint).toContain('accumulated tick note survives the closing tool call');

    // The interrupt/post-turn operator path now records pickup-lag telemetry (it
    // previously passed no created_at → the lag was invisible in the summary).
    const opLatency = vi.mocked(recordResidentTurnLatency).mock.calls
      .map((c) => c[0])
      .find((rec) => rec.kind === 'operator');
    expect(opLatency).toBeDefined();
    expect(typeof opLatency?.pickupLagMs).toBe('number');
    expect(opLatency?.pickupLagMs as number).toBeGreaterThanOrEqual(0);
  });

  it('restores operatorDecisionCorrectiveNext from the persisted ledger so a full restart still fires the corrective reminder on the next autonomous turn', async () => {
    // Full-restart durability: a marker-less operator correction set
    // operatorDecisionCorrectiveNext=true and it was serialized into the durable
    // exam-ledger blob (task.context._exam_ledger) on the last heartbeat. After a
    // VM recycle / crash the fresh session must restore that flag and open the
    // FIRST autonomous tick with the OPERATOR-DECISION CORRECTIVE reminder — before
    // the fix the flag reset to false and the reminder was silently dropped.
    let autonomousPrompt = '';
    let alive = true;

    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/api/agent/git-read-token')) {
        return Promise.resolve({ token: 'read-scoped-token' });
      }
      if (url.includes('/messages?')) return Promise.resolve({ messages: [] });
      return Promise.resolve(null);
    });
    mockApiPost.mockResolvedValue({ authority_held: true });

    const session1 = makeSession(
      (text) => {
        // First (and only) turn is the autonomous tick with the bootstrap prompt
        // prepended (runTurn prepends it on the first turn). Capture it, then let
        // the session die so the loop exits on the next iteration.
        autonomousPrompt = typeof text === 'string' ? text : JSON.stringify(text);
        alive = false;
        return Promise.resolve(turnResult({ resultText: 'looked around', inputTokens: 0 }));
      },
      () => alive,
    );
    mockStartSession.mockImplementation(() => session1);

    await handleStrategicResident(
      'task-restore',
      { project_name: 'Proj', open_messages: [] },
      {
        project_metadata: { project_id: 'proj-1' },
        // The durable blob a prior session persisted after a marker-less operator
        // correction: operatorDecisionCorrective=true rides the same channel as the
        // exam ledger's records/tick/corrective fields.
        context: {
          _exam_ledger: JSON.stringify({
            records: [], tick: 4, corrective: false, operatorDecisionCorrective: true,
          }),
        },
      },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    // The restored flag drove the corrective frame into the first autonomous prompt.
    expect(autonomousPrompt).toContain('OPERATOR-DECISION REVIEW');
  });

  it('re-clones the vanished workspace with a FRESH READ token, never the session-start push token', async () => {
    // Override the default existsSync=true spy so the workspace-vanish guard fires.
    vi.spyOn(fs, 'existsSync').mockReturnValue(false);
    mockPrepareWorkspaceForRole.mockReset();
    mockWriteGitTokenFile.mockReset();

    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/api/agent/git-read-token')) {
        return Promise.resolve({ token: 'fresh-read-token' });
      }
      if (url.includes('/messages?')) return Promise.resolve({ messages: [] });
      return Promise.resolve(null);
    });
    mockApiPost.mockResolvedValue({ authority_held: true });

    // Each startResidentClaudeSession call returns a new dead session: startup
    // session + recovery session (post-guard restart). Both die immediately so the
    // loop exits after the recovery prime.
    mockStartSession.mockImplementation(() => makeSession(
      () => Promise.resolve(turnResult({ resultText: 'x', inputTokens: 0 })),
      () => false,
    ));

    await handleStrategicResident(
      'task-rc', { project_name: 'P', open_messages: [] },
      { project_metadata: { project_id: 'p' }, context: {} },
      'PUSH-CAPABLE-GHTOK', 'HenryHamster', 'BlueprintAIStudio', null, { value: '' },
    );

    // The re-clone (the LAST prepareWorkspaceForRole call) used the fresh read
    // token, NOT the push-capable session-start ghtok.
    const calls = mockPrepareWorkspaceForRole.mock.calls;
    const reclone = calls[calls.length - 1][0];
    expect(reclone.githubToken).toBe('fresh-read-token');
    expect(reclone.repoUrl).toContain('fresh-read-token');
    expect(reclone.repoUrl).not.toContain('PUSH-CAPABLE-GHTOK');
    // The read-token endpoint was consulted for the re-clone; the push endpoint never.
    const urls = mockApiGet.mock.calls.map((c) => c[0]);
    expect(urls).toContain('/api/agent/git-read-token');
    expect(urls.some((u) => u.includes('/api/agent/github/token'))).toBe(false);
  });

  it('re-provisions helper tools and restarts the subprocess after workspace vanish', async () => {
    // Override existsSync to simulate workspace vanish (guard fires exactly once).
    vi.spyOn(fs, 'existsSync').mockReturnValue(false);
    mockWritePlaybookHelper.mockReset();
    mockWriteStrategicHelpers.mockReset();
    mockProvisionAmbientHelpers.mockReset();

    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/api/agent/git-read-token')) {
        return Promise.resolve({ token: 'fresh-read-token' });
      }
      if (url.includes('/messages?')) return Promise.resolve({ messages: [] });
      return Promise.resolve(null);
    });
    mockApiPost.mockResolvedValue({ authority_held: true });

    // Two sessions: session1 from startup, session2 from recovery restart.
    const session1 = makeSession(
      () => Promise.resolve(turnResult({ resultText: '', inputTokens: 0 })),
      () => false,
    );
    const session2 = makeSession(
      () => Promise.resolve(turnResult({ resultText: '', inputTokens: 0 })),
      () => false,
    );
    mockStartSession
      .mockReturnValueOnce(session1)
      .mockReturnValueOnce(session2);

    await handleStrategicResident(
      'task-recovery', { project_name: 'P', open_messages: [] },
      { project_metadata: { project_id: 'p' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    // subprocess restarted: startResidentClaudeSession called once at startup + once for recovery
    expect(mockStartSession).toHaveBeenCalledTimes(2);
    // old subprocess was closed before the new one was started
    // eslint-disable-next-line @typescript-eslint/unbound-method
    expect(session1.close).toHaveBeenCalled();
    // recovery session received the prime (compaction checkpoint)
    // eslint-disable-next-line @typescript-eslint/unbound-method
    expect(session2.sendTurn).toHaveBeenCalledTimes(1);
    // helpers were re-provisioned into the recreated checkout (startup + recovery = 2 calls each)
    expect(mockWritePlaybookHelper.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(mockWriteStrategicHelpers.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(mockProvisionAmbientHelpers.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it('stops gracefully when the workspace-recovery prime returns processGone', async () => {
    // Simulate workspace vanish so the guard fires and the recovery prime runs.
    vi.spyOn(fs, 'existsSync').mockReturnValue(false);
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/api/agent/git-read-token')) {
        return Promise.resolve({ token: 'fresh-read-token' });
      }
      if (url.includes('/messages?')) return Promise.resolve({ messages: [] });
      return Promise.resolve(null);
    });
    mockApiPost.mockResolvedValue({ authority_held: true });

    const session1 = makeSession(
      () => Promise.resolve(turnResult()),
      () => true,
    );
    const session2 = makeSession(
      () => Promise.resolve(turnResult({ processGone: true })),
      () => true,
    );
    mockStartSession
      .mockReturnValueOnce(session1)
      .mockReturnValueOnce(session2);

    // Should resolve without throwing even when the prime fails.
    await expect(handleStrategicResident(
      'task-prime-fail', { project_name: 'P', open_messages: [] },
      { project_metadata: { project_id: 'p' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    )).resolves.toBeUndefined();

    // The prime was the only turn on session2 — no operator/autonomous turns followed.
    // eslint-disable-next-line @typescript-eslint/unbound-method
    expect(session2.sendTurn).toHaveBeenCalledTimes(1);
  });

  it('preserves the advanced message cursor across workspace reclone', async () => {
    // Override the default existsSync=true spy so the workspace-vanish guard fires.
    vi.spyOn(fs, 'existsSync').mockReturnValue(false);
    const initialCursor = '2026-01-01T00:00:00.000000';
    const advancedCursor = '2026-07-15T14:00:00.000000Z';
    const operatorMessage = {
      id: 'msg-cursor-check',
      content: 'operator message after the initial cursor',
      created_at: advancedCursor,
      from_type: 'user',
    };
    let messagePollCount = 0;

    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/api/agent/git-read-token')) {
        return Promise.resolve({ token: 'read-scoped-token' });
      }
      if (url.includes('/messages?')) {
        return Promise.resolve({
          messages: messagePollCount++ === 0 ? [operatorMessage] : [],
        });
      }
      if (url.includes('checkpoint-state')) {
        return Promise.resolve({
          problem_model: {},
          vision: {},
          strategic_memory_notes: [],
          running_update: '',
          open_messages: [],
        });
      }
      return Promise.resolve(null);
    });
    mockPrepareWorkspaceForRole.mockClear();
    mockApiPost.mockResolvedValue({ authority_held: true });

    let turnCount = 0;
    const session = makeSession(
      () => {
        turnCount += 1;
        return Promise.resolve(turnResult({
          resultText: turnCount === 1
            ? 'acknowledged the operator message'
            : 'completed one follow-through tick',
          inputTokens: 50,
        }));
      },
      () => turnCount < 2,
    );
    mockStartSession.mockReturnValue(session);

    await handleStrategicResident(
      'task-cursor-reclone',
      { project_name: 'CursorProj', open_messages: [] },
      {
        project_metadata: { project_id: 'proj-cursor' },
        context: { message_cursor: initialCursor },
      },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    const heartbeatCursors = mockApiPost.mock.calls
      .filter(([url]) => url.includes('heartbeat'))
      .map(([, body]) => (body as Record<string, unknown>).message_cursor as string | undefined);
    const lastCursor = heartbeatCursors.at(-1);

    expect(heartbeatCursors.length).toBeGreaterThan(0);
    expect(turnCount).toBe(2);
    expect(mockPrepareWorkspaceForRole.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(lastCursor).toBeDefined();
    expect(lastCursor).not.toBe(initialCursor);
    expect(new Date(lastCursor ?? 0).getTime()).toBeGreaterThanOrEqual(
      new Date(advancedCursor).getTime(),
    );
  });

  it('restores the modern operatorDecisionReview object from the persisted exam ledger so OPERATOR-DECISION REVIEW fires with message-id metadata on the first autonomous tick after a full restart', async () => {
    // Full-restart durability for the modern operatorDecisionReview object path.
    //
    // A prior session serialized a PendingOperatorDecisionReview (the modern
    // format, with messageIds / startedAt / attempts) into task.context._exam_ledger.
    // After VM recycle the fresh session must:
    //   1. restore the review object from the durable blob via restoreLedger, and
    //   2. open the FIRST autonomous tick with the OPERATOR-DECISION REVIEW
    //      instruction AND the per-review metadata line (message id, attempts).
    //
    // This is the direct-object path; the sibling test above covers the legacy
    // boolean (operatorDecisionCorrective: true → synthetic review) path.
    let autonomousPrompt = '';
    let alive = true;

    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/api/agent/git-read-token')) {
        return Promise.resolve({ token: 'read-scoped-token' });
      }
      if (url.includes('/messages?')) return Promise.resolve({ messages: [] });
      return Promise.resolve(null);
    });
    mockApiPost.mockResolvedValue({ authority_held: true });

    const session = makeSession(
      (text) => {
        autonomousPrompt = typeof text === 'string' ? text : JSON.stringify(text);
        alive = false;
        return Promise.resolve(turnResult({ resultText: 'reviewed the operator exchange', inputTokens: 0 }));
      },
      () => alive,
    );
    mockStartSession.mockImplementation(() => session);

    await handleStrategicResident(
      'task-review-restore',
      { project_name: 'ReviewProj', open_messages: [] },
      {
        project_metadata: { project_id: 'proj-review' },
        // Modern serialized review: an operator exchange with explicit message ids
        // and one prior attempt is pending classification.
        context: {
          _exam_ledger: JSON.stringify({
            records: [],
            tick: 5,
            corrective: false,
            operatorDecisionReview: {
              status: 'pending',
              messageIds: ['msg-op-review-1', 'msg-op-review-2'],
              startedAt: '2026-07-15T10:00:00Z',
              attempts: 1,
            },
          }),
        },
      },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    // The restored review drove the OPERATOR-DECISION REVIEW frame into the
    // first autonomous prompt, including the message-id metadata line.
    expect(autonomousPrompt).toContain('OPERATOR-DECISION REVIEW');
    expect(autonomousPrompt).toContain('msg-op-review-1');
  });

  it('compaction checkpoint carries contextPrefix (operator purpose + constraints) from buildContextPrefix', async () => {
    // Finding: buildBootstrapPrompt receives contextPrefix but CompactionCheckpointContext
    // formerly had no corresponding field — a compacted session would operate without
    // the operator purpose / constraints preamble. This test verifies the integration:
    // contextPrefix from buildContextPrefix flows into the checkpoint primed into the
    // fresh subprocess.
    const CONTEXT_PREFIX = 'Operator Purpose: build great software.\nConstraints: do not break prod.\nCurrent focus: stabilize v26.';
    mockBuildContextPrefix.mockReturnValue(CONTEXT_PREFIX);

    let capturedCheckpoint = '';
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/api/agent/git-read-token')) {
        return Promise.resolve({ token: 'read-scoped-token' });
      }
      if (url.includes('/messages?')) return Promise.resolve({ messages: [] });
      if (url.includes('checkpoint-state')) {
        return Promise.resolve({
          problem_model: {}, vision: {}, strategic_memory_notes: [], running_update: '', open_messages: [],
        });
      }
      return Promise.resolve(null);
    });
    mockApiPost.mockResolvedValue({ authority_held: true });

    // Session 1: high input tokens to trip compaction (threshold 100).
    // Session 2: post-compaction subprocess — captures the checkpoint, then dies.
    const session1 = makeSession(
      () => Promise.resolve(turnResult({ resultText: 'did some work', inputTokens: 5_000 })),
      () => true,
    );
    const session2 = makeSession(
      (text) => {
        capturedCheckpoint = typeof text === 'string' ? text : JSON.stringify(text);
        return Promise.resolve(turnResult({ resultText: 'primed', inputTokens: 0 }));
      },
      () => false, // dead ⇒ loop exits
    );
    let spawnCount = 0;
    mockStartSession.mockImplementation(() => {
      spawnCount += 1;
      return spawnCount === 1 ? session1 : session2;
    });

    await handleStrategicResident(
      'task-ctx-prefix-compaction',
      {
        project_name: 'Proj',
        open_messages: [],
        strategic_session_compaction_threshold_tokens: 100,
      },
      { project_metadata: { project_id: 'proj-ctx' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    // The compacted session was primed — it received the checkpoint.
    expect(capturedCheckpoint.length).toBeGreaterThan(0);
    // The checkpoint must contain the operator purpose / constraints preamble.
    expect(capturedCheckpoint).toContain('Operator Purpose: build great software.');
    expect(capturedCheckpoint).toContain('Constraints: do not break prod.');
    expect(capturedCheckpoint).toContain('stabilize v26');
  });

  it('workspace recovery checkpoint carries contextPrefix so the restarted subprocess has operator grounding', async () => {
    // Finding: the subprocess spawned after workspace recovery was primed with a
    // compaction checkpoint that lacked contextPrefix — the restored session would
    // operate without the operator purpose / constraints preamble. This test verifies
    // contextPrefix flows into the recovery prime.
    const CONTEXT_PREFIX = 'Operator Purpose: autonomous research.\nConstraints: no prod writes.\nCurrent focus: v26 fix wave.';
    mockBuildContextPrefix.mockReturnValue(CONTEXT_PREFIX);

    vi.spyOn(fs, 'existsSync').mockReturnValue(false);

    let capturedCheckpoint = '';
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/api/agent/git-read-token')) {
        return Promise.resolve({ token: 'fresh-read-token' });
      }
      if (url.includes('/messages?')) return Promise.resolve({ messages: [] });
      return Promise.resolve(null);
    });
    mockApiPost.mockResolvedValue({ authority_held: true });

    const session1 = makeSession(
      () => Promise.resolve(turnResult({ resultText: '', inputTokens: 0 })),
      () => false,
    );
    const session2 = makeSession(
      (text) => {
        capturedCheckpoint = typeof text === 'string' ? text : JSON.stringify(text);
        return Promise.resolve(turnResult({ resultText: 'primed after recovery', inputTokens: 0 }));
      },
      () => false,
    );
    mockStartSession
      .mockReturnValueOnce(session1)
      .mockReturnValueOnce(session2);

    await handleStrategicResident(
      'task-ctx-prefix-recovery',
      { project_name: 'RecoveryProj', open_messages: [] },
      { project_metadata: { project_id: 'proj-recovery' }, context: {} },
      'PUSH-CAPABLE-GHTOK', 'owner', 'repo', null, { value: '' },
    );

    // The recovery subprocess received a prime with the checkpoint.
    // eslint-disable-next-line @typescript-eslint/unbound-method
    expect(session2.sendTurn).toHaveBeenCalledTimes(1);
    // The checkpoint must carry the operator purpose / constraints.
    expect(capturedCheckpoint).toContain('Operator Purpose: autonomous research.');
    expect(capturedCheckpoint).toContain('Constraints: no prod writes.');
    expect(capturedCheckpoint).toContain('v26 fix wave');
  });
});

// ── Ambient git credential must stay READ-SCOPED (audit finding #10) ───────────
// The resident is a long-lived session, so the token in its credential file is
// ambient for the whole workday. It must be minted from the read-scoped
// /api/agent/git-read-token, never from the push-capable /api/agent/github/token
// — the latter left a standing push-capable installation token in the file,
// re-minted every 8 minutes.

describe('refreshResidentGitReadToken — the resident holds no ambient push-capable token', () => {
  beforeEach(() => {
    mockApiGet.mockReset();
    mockSendLog.mockReset();
    mockWriteGitTokenFile.mockReset();
  });

  it('refreshes from the READ-SCOPED endpoint and never from the push-capable one', async () => {
    mockApiGet.mockResolvedValue({ token: 'read-scoped-token', owner: 'o', repo: 'r' });

    await refreshResidentGitReadToken(null);

    const urls = mockApiGet.mock.calls.map((c) => c[0]);
    expect(urls).toEqual(['/api/agent/git-read-token']);
    // The push-capable installation-token endpoint must never be reached from the
    // background refresh — that is the standing privilege escalation itself.
    expect(urls.some((u) => u.includes('/api/agent/github/token'))).toBe(false);
    // …and the token written into the ambient credential file is the read-scoped one.
    expect(mockWriteGitTokenFile).toHaveBeenCalledWith('read-scoped-token');
  });

  it('keeps the existing token when the refresh returns nothing', async () => {
    mockApiGet.mockResolvedValue(null);

    await refreshResidentGitReadToken(null);

    expect(mockWriteGitTokenFile).not.toHaveBeenCalled();
  });

  it('never throws when the refresh fails, so the resident loop survives', async () => {
    mockApiGet.mockRejectedValue(new Error('502 bad gateway'));

    await expect(refreshResidentGitReadToken(null)).resolves.toBeUndefined();
    expect(mockWriteGitTokenFile).not.toHaveBeenCalled();
  });
});

// ── Mid-session "workspace vanished" re-clone must be consistent with the above ─
// The guard re-cloned with the cloneUrl/ghToken captured at session START: a
// PUSH-capable token (which the resident must never hold ambiently) that has also
// EXPIRED by then (installation tokens live 60 min; the resident runs for hours),
// so the re-clone threw and killed the session precisely when it was needed most.

describe('recloneResidentWorkspace — fresh READ-scoped credential at the moment of use', () => {
  beforeEach(() => {
    mockApiGet.mockReset();
    mockApiPost.mockReset();
    mockStartSession.mockReset();
    mockSendLog.mockReset();
    mockWriteGitTokenFile.mockReset();
    mockSetupGitCredentialHelper.mockReset();
    mockPrepareWorkspaceForRole.mockReset();
    vi.spyOn(fs, 'existsSync').mockReturnValue(true);
  });
  afterEach(() => { vi.restoreAllMocks(); });

  it('mints a FRESH read-scoped token and clones with it — never the stale push token', async () => {
    mockApiGet.mockResolvedValue({ token: 'fresh-read-token' });

    await recloneResidentWorkspace(null, 'HenryHamster', 'BlueprintAIStudio', 'automain');

    // The token comes from the READ-scoped endpoint, minted now — not the
    // session-start push-capable one, and not the push endpoint.
    const urls = mockApiGet.mock.calls.map((c) => c[0]);
    expect(urls).toEqual(['/api/agent/git-read-token']);
    expect(urls.some((u) => u.includes('/api/agent/github/token'))).toBe(false);

    const opts = mockPrepareWorkspaceForRole.mock.calls[0][0];
    expect(opts.repoUrl).toBe(
      'https://x-access-token:fresh-read-token@github.com/HenryHamster/BlueprintAIStudio.git',
    );
    expect(opts.githubToken).toBe('fresh-read-token');
    expect(opts.branch).toBe('automain');
    expect(opts.depth).toBe(0);
  });

  it('restores the refreshable credential helper so fetch survives the next TTL', async () => {
    mockApiGet.mockResolvedValue({ token: 'fresh-read-token' });

    await recloneResidentWorkspace(null, 'o', 'r', 'main');

    // Without these the re-cloned workspace keeps a token-embedded remote that the
    // 8-minute background refresh cannot update — fetches break at the next TTL.
    expect(mockWriteGitTokenFile).toHaveBeenCalledWith('fresh-read-token');
    expect(mockSetupGitCredentialHelper).toHaveBeenCalledWith('o', 'r', expect.anything());
  });

  it('calls recordOperatorDecisionCapture(true) when operator turn has OPERATOR-DECISION marker', async () => {
    const OPERATOR_MSG = {
      id: 'msg-captured',
      content: 'Use approach Y instead.',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let pollCount = 0;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/api/agent/git-read-token')) {
        return Promise.resolve({ token: 'read-scoped-token' });
      }
      if (url.includes('/messages?')) {
        if (pollCount++ === 0) return Promise.resolve({ messages: [OPERATOR_MSG] });
        return Promise.resolve({ messages: [] });
      }
      if (url.includes('checkpoint-state')) {
        return Promise.resolve({ problem_model: {}, vision: {}, strategic_memory_notes: [], running_update: '' });
      }
      return Promise.resolve(null);
    });
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('heartbeat')) {
        return Promise.resolve({ authority_held: true, stop_requested: true });
      }
      if (url.includes('strategic')) {
        return Promise.resolve({});
      }
      return Promise.resolve({});
    });
    const session = makeSession(
      () => Promise.resolve(turnResult({
        resultText: 'I agree, approach Y is better.',
        accumulatedTurnText: 'OPERATOR-DECISION: use approach Y, not X',
      })),
      () => true,
    );
    mockStartSession.mockReturnValue(session);

    await handleStrategicResident(
      'task-cap', {}, { project_metadata: { project_id: 'proj-cap' } },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    expect(mockApiPost).toHaveBeenLastCalledWith(
      expect.stringContaining('heartbeat'),
      expect.objectContaining({ handled_message_ids: ['msg-captured'] }),
    );
    expect(recordOperatorDecisionCapture).toHaveBeenCalledWith('captured');
  });

  it('keeps an earlier markerless exchange pending when a later marked turn arrives before the corrective tick', async () => {
    // The later marked turn must not overwrite or consume the first exchange's
    // durable review, and each completed turn contributes once to marker coverage.
    const OPERATOR_MSG = {
      id: 'msg-nomarker',
      content: 'Try approach Z.',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    const MARKED_MSG = {
      id: 'msg-marked', content: 'Keep approach Y.',
      created_at: new Date(Date.now() + 1).toISOString(), from_type: 'user',
    };
    let pollCount = 0;
    let heartbeatNote = '';
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/api/agent/git-read-token')) {
        return Promise.resolve({ token: 'read-scoped-token' });
      }
      if (url.includes('/messages?')) {
        const messages = pollCount++ === 0 ? [OPERATOR_MSG]
          : pollCount === 2 ? [MARKED_MSG] : [];
        return Promise.resolve({ messages });
      }
      if (url.includes('checkpoint-state')) {
        return Promise.resolve({ problem_model: {}, vision: {}, strategic_memory_notes: [], running_update: '' });
      }
      return Promise.resolve(null);
    });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('heartbeat')) {
        // The operator-turn heartbeat lands BEFORE we stop; capture its serialized
        // exam ledger so we can prove the corrective flag was armed.
        if (typeof body?.note === 'string') heartbeatNote = body.note;
        return Promise.resolve({ authority_held: true, stop_requested: pollCount >= 2 });
      }
      if (url.includes('strategic')) {
        return Promise.resolve({});
      }
      return Promise.resolve({});
    });
    let turn = 0;
    const session = makeSession(
      () => Promise.resolve(turnResult({ resultText: turn++ === 0
        ? 'OK, will examine approach Z.'
        : 'Confirmed.\nOPERATOR-DECISION: Keep approach Y.',
      })),
      () => true,
    );
    mockStartSession.mockReturnValue(session);

    mockSendWsFrame.mockReset();
    mockSendWsFrame.mockReturnValue(true);
    vi.mocked(recordOperatorDecisionCapture).mockClear();

    await handleStrategicResident(
      'task-nm', {}, { project_metadata: { project_id: 'proj-nm' } },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    expect(vi.mocked(recordOperatorDecisionCapture).mock.calls).toEqual([
      ['markerless'], ['captured'],
    ]);
    expect(heartbeatNote).toContain('"operatorDecisionCorrective":true');
    expect(heartbeatNote).toContain('"messageIds":["msg-nomarker"]');
    expect(heartbeatNote).not.toContain('"messageIds":["msg-marked"]');
  });
});

// ── Autonomous corrective tick: recover a missed operator decision, reorder
// heartbeat vs classification/admission (defects 1, 2, 4). Restoring
// operatorDecisionCorrective=true from the
// persisted ledger makes the FIRST autonomous tick the marker-miss corrective.
describe('handleStrategicResident — autonomous marker-miss corrective loop', () => {
  beforeEach(() => {
    mockApiGet.mockReset();
    mockApiPost.mockReset();
    mockStartSession.mockReset();
    mockSendLog.mockReset();
    mockSendWsFrame.mockReset();
    mockSendWsFrame.mockReturnValue(true);
    vi.mocked(recordOperatorDecisionCapture).mockClear();
    vi.spyOn(fs, 'existsSync').mockReturnValue(true);
  });
  afterEach(() => { vi.restoreAllMocks(); });

  /** Build a task whose context restores operatorDecisionCorrective=true so the
   *  first autonomous tick runs as the marker-miss corrective. */
  const correctiveTask = (): Record<string, unknown> => ({
    project_metadata: { project_id: 'proj-corr' },
    context: {
      _exam_ledger: JSON.stringify({
        records: [], tick: 7, corrective: false, operatorDecisionCorrective: true,
      }),
    },
  });

  it('recovers a missed OPERATOR-DECISION from the corrective tick, admits it to the durable register, and the heartbeat carries it (classification+admission run BEFORE the heartbeat)', async () => {
    const RECOVERED_NOTE = 'operator settled on approach X — was said in chat without the marker';
    let alive = true;
    const heartbeatBodies: Array<Record<string, unknown>> = [];

    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/api/agent/git-read-token')) {
        return Promise.resolve({ token: 'read-scoped-token' });
      }
      if (url.includes('/messages?')) return Promise.resolve({ messages: [] });
      return Promise.resolve(null);
    });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('heartbeat')) {
        if (body) heartbeatBodies.push(body);
        return Promise.resolve({ authority_held: true });
      }
      return Promise.resolve({});
    });

    const session = makeSession(
      () => {
        // The corrective autonomous tick recovers the decision the operator
        // forgot to mark. appliedStrategicOp so no trailing pace-sleep; die so the
        // loop exits after this single tick's heartbeat has landed.
        alive = false;
        return Promise.resolve(turnResult({
          resultText: `Re-checked the recent operator conversation.\nOPERATOR-DECISION: ${RECOVERED_NOTE}`,
          appliedStrategicOp: true,
          inputTokens: 0,
        }));
      },
      () => alive,
    );
    mockStartSession.mockReturnValue(session);

    await handleStrategicResident(
      'task-corr', { project_name: 'Proj', open_messages: [] }, correctiveTask(),
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    // A confirmed miss was recorded (evidence-grounded: recovered on the tick).
    expect(recordOperatorDecisionCapture).not.toHaveBeenCalled();

    // Defect 4: the post-turn heartbeat carries BOTH the just-classified exam
    // record AND the newly recovered decision — proving classification + admission
    // ran BEFORE the heartbeat (no restart window).
    const withDecision = heartbeatBodies.find(
      (b): b is Record<string, unknown> & { decisions_reg: string; note: string } =>
        typeof b.decisions_reg === 'string' && b.decisions_reg.includes(RECOVERED_NOTE)
        && typeof b.note === 'string',
    );
    expect(withDecision).toBeDefined();
    const reg = JSON.parse(withDecision?.decisions_reg ?? '{}') as {
      entries: Array<{ surfaceKey: string; verdict: string; note: string }>;
    };
    const recovered = reg.entries.find((e) => e.surfaceKey === 'operator:message');
    expect(recovered).toEqual(
      expect.objectContaining({ verdict: 'operator', note: RECOVERED_NOTE }),
    );
    // The same heartbeat's exam ledger carries the just-pushed record (tick 8).
    const ledger = JSON.parse(withDecision?.note ?? '{}') as { tick: number };
    expect(ledger.tick).toBe(8);
  });

  it('records a capture (true) — NOT a miss — when the corrective tick completes cleanly with no recovered decision', async () => {
    let alive = true;
    let heartbeatNote = '';

    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/api/agent/git-read-token')) {
        return Promise.resolve({ token: 'read-scoped-token' });
      }
      if (url.includes('/messages?')) return Promise.resolve({ messages: [] });
      return Promise.resolve(null);
    });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('heartbeat')) {
        if (typeof body?.note === 'string') heartbeatNote = body.note;
        return Promise.resolve({ authority_held: true });
      }
      return Promise.resolve({});
    });

    const session = makeSession(
      () => {
        alive = false;
        // Clean completion, no OPERATOR-DECISION marker → the operator's earlier
        // markerless turn was routine chat; nothing was actually missed.
        return Promise.resolve(turnResult({
          resultText: 'Reviewed the recent conversation.\nOPERATOR-DECISION-REVIEW: NO-DECISION\nTICK-VERDICT: CLEAR — nothing actionable',
          appliedStrategicOp: true,
          inputTokens: 0,
        }));
      },
      () => alive,
    );
    mockStartSession.mockReturnValue(session);

    await handleStrategicResident(
      'task-corr2', { project_name: 'Proj', open_messages: [] }, correctiveTask(),
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    expect(recordOperatorDecisionCapture).not.toHaveBeenCalled();
    // The corrective was resolved → the flag is cleared in the serialized ledger.
    expect(heartbeatNote).toContain('"operatorDecisionCorrective":false');
  });

  it('keeps operatorDecisionCorrectiveNext armed across an INTERRUPTED corrective tick and fires it again on the next autonomous tick', async () => {
    // Defect 2: the corrective flag must NOT be consumed before the turn succeeds.
    // Turn 1 is interrupted (no recovery, inconclusive) → record nothing, flag
    // survives (re-serialized in the heartbeat) → turn 2 re-runs the corrective.
    let turn = 0;
    let alive = true;
    const autonomousPrompts: string[] = [];
    let interruptedHeartbeatNote = '';
    // Increment work_state_rev on every heartbeat so the post-turn rev differs
    // from the liveness rev observed during gating — deterministically firing the
    // second autonomous tick without waiting on the floor interval.
    let rev = 0;

    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/api/agent/git-read-token')) {
        return Promise.resolve({ token: 'read-scoped-token' });
      }
      if (url.includes('/messages?')) return Promise.resolve({ messages: [] });
      return Promise.resolve(null);
    });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('heartbeat')) {
        rev += 1;
        // Capture the exam-ledger note from the FIRST (post-interrupt) heartbeat.
        if (turn === 1 && typeof body?.note === 'string' && !interruptedHeartbeatNote) {
          interruptedHeartbeatNote = body.note;
        }
        return Promise.resolve({ authority_held: true, work_state_rev: `rev-${rev}` });
      }
      return Promise.resolve({});
    });

    const session = makeSession(
      (text) => {
        turn += 1;
        autonomousPrompts.push(typeof text === 'string' ? text : JSON.stringify(text));
        if (turn === 1) {
          // Interrupted mid-thought before it could recover the decision.
          return Promise.resolve(turnResult({
            resultText: 'OPERATOR-DECISION: partial marker must not settle',
            interrupted: true, sawToolUse: false, inputTokens: 0,
          }));
        }
        // Turn 2: complete cleanly (no recovery) and die so the loop exits.
        alive = false;
        return Promise.resolve(turnResult({
          resultText: 'Second look — still nothing pending.\nTICK-VERDICT: CLEAR — nothing actionable',
          appliedStrategicOp: true, inputTokens: 0,
        }));
      },
      () => alive,
    );
    mockStartSession.mockReturnValue(session);

    await handleStrategicResident(
      'task-corr3',
      // Small tick intervals keep the between-turn gating wait short.
      {
        project_name: 'Proj', open_messages: [],
        strategic_session_idle_tick_seconds: 1,
        strategic_session_message_poll_seconds: 0.75,
      },
      correctiveTask(),
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    // Two autonomous ticks ran.
    expect(turn).toBe(2);
    // Turn 1 was the corrective (restored flag).
    expect(autonomousPrompts[0]).toContain('OPERATOR-DECISION REVIEW');
    expect(recordOperatorDecisionCapture).not.toHaveBeenCalled();
    // Defect 2: the flag survived the interrupt in the serialized ledger…
    expect(interruptedHeartbeatNote).toContain('"operatorDecisionCorrective":true');
    // …and re-fired the corrective on the SECOND autonomous tick.
    expect(autonomousPrompts[1]).toContain('OPERATOR-DECISION REVIEW');
  }, 10_000);
});

describe('recloneResidentWorkspace', () => {
  beforeEach(() => {
    mockApiGet.mockReset();
    mockPrepareWorkspaceForRole.mockReset();
    mockWriteGitTokenFile.mockReset();
    mockSetupGitCredentialHelper.mockReset();
  });

  it('throws rather than re-cloning when no read token can be minted', async () => {
    mockApiGet.mockResolvedValue(null);

    await expect(
      recloneResidentWorkspace(null, 'o', 'r', 'main'),
    ).rejects.toThrow(/git-read-token/);
    // It must NOT fall back to any other credential: no clone, and nothing new
    // parked in the ambient credential file.
    expect(mockPrepareWorkspaceForRole).not.toHaveBeenCalled();
    expect(mockWriteGitTokenFile).not.toHaveBeenCalled();
  });
});

// ── Early operator-turn acknowledgment ───────────────────────────────────────
// When an operator turn runs longer than the configured threshold, the handler
// must post a durable "on it" message so the operator gets a progress signal
// during a slow turn rather than silence for the full duration. The ack is
// suppressed entirely for fast turns (timer is cleared before it fires).

describe('handleStrategicResident — early operator-turn acknowledgment', () => {
  beforeEach(() => {
    mockApiGet.mockReset();
    mockApiPost.mockReset();
    mockStartSession.mockReset();
    mockSendLog.mockReset();
    mockSendWsFrame.mockReset();
    mockSendWsFrame.mockReturnValue(true);
    vi.spyOn(fs, 'existsSync').mockReturnValue(true);
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('posts an early acknowledgment when the operator turn exceeds the configured threshold', async () => {
    vi.useFakeTimers();

    const OPERATOR_MSG = {
      id: 'msg-slow',
      content: 'Question that will take a while to answer',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let pollCount = 0;
    let resolveTurn!: (r: ResidentTurnResult) => void;
    const blockedTurnPromise = new Promise<ResidentTurnResult>((resolve) => { resolveTurn = resolve; });

    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/api/agent/git-read-token')) return Promise.resolve({ token: 'read-scoped-token' });
      if (url.includes('/messages?')) {
        if (pollCount++ === 0) return Promise.resolve({ messages: [OPERATOR_MSG] });
        return Promise.resolve({ messages: [] });
      }
      return Promise.resolve(null);
    });

    const strategicPosts: Array<Record<string, unknown>> = [];
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/api/agent/strategic') && body?.op === 'create_message') {
        strategicPosts.push(body);
      }
      if (url.includes('heartbeat')) return Promise.resolve({ authority_held: true, stop_requested: true });
      return Promise.resolve({});
    });

    const session = makeSession(() => blockedTurnPromise, () => true);
    mockStartSession.mockReturnValue(session);

    // 2s threshold (above the 1s floor) — should fire during the slow turn
    const handlerDone = handleStrategicResident(
      'task-early-ack',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_operator_ack_threshold_seconds: 2,
      },
      { project_metadata: { project_id: 'proj-ack' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    // Advance past the threshold — ack timer fires at 2000ms
    await vi.advanceTimersByTimeAsync(3000);

    // The early ack must have been posted
    expect(strategicPosts.length).toBeGreaterThanOrEqual(1);
    expect(JSON.stringify(strategicPosts[0])).toContain('On it');

    // Resolve the slow turn; handler should proceed to the heartbeat and stop
    resolveTurn(turnResult({ sentOperatorMessage: true }));
    await vi.advanceTimersByTimeAsync(1000);
    await handlerDone;
  });

  it('does not post an early ack when the turn completes before the threshold elapses', async () => {
    vi.useFakeTimers();

    const OPERATOR_MSG = {
      id: 'msg-fast',
      content: 'Quick question',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let pollCount = 0;

    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/api/agent/git-read-token')) return Promise.resolve({ token: 'read-scoped-token' });
      if (url.includes('/messages?')) {
        if (pollCount++ === 0) return Promise.resolve({ messages: [OPERATOR_MSG] });
        return Promise.resolve({ messages: [] });
      }
      return Promise.resolve(null);
    });

    const ackPosts: Array<Record<string, unknown>> = [];
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/api/agent/strategic') && body?.op === 'create_message') {
        ackPosts.push(body);
      }
      if (url.includes('heartbeat')) return Promise.resolve({ authority_held: true, stop_requested: true });
      return Promise.resolve({});
    });

    // Turn resolves immediately — before any 10s timer can fire
    const session = makeSession(
      () => Promise.resolve(turnResult({ sentOperatorMessage: true })),
      () => true,
    );
    mockStartSession.mockReturnValue(session);

    await handleStrategicResident(
      'task-fast-ack',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_operator_ack_threshold_seconds: 10,
      },
      { project_metadata: { project_id: 'proj-fast-ack' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    // Advance well past the 10s threshold to confirm the timer was cancelled, not just unfired
    await vi.advanceTimersByTimeAsync(15_000);
    expect(ackPosts.length).toBe(0);
  });

  it('completes normally when the early ack post fails', async () => {
    vi.useFakeTimers();

    const OPERATOR_MSG = {
      id: 'msg-ack-fail',
      content: 'Message where ack will fail',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let pollCount = 0;
    let resolveTurn!: (r: ResidentTurnResult) => void;
    const blockedTurnPromise = new Promise<ResidentTurnResult>((resolve) => { resolveTurn = resolve; });

    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/api/agent/git-read-token')) return Promise.resolve({ token: 'read-scoped-token' });
      if (url.includes('/messages?')) {
        if (pollCount++ === 0) return Promise.resolve({ messages: [OPERATOR_MSG] });
        return Promise.resolve({ messages: [] });
      }
      return Promise.resolve(null);
    });

    let heartbeatCount = 0;
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      // Ack post fails — must be non-fatal
      if (url.includes('/api/agent/strategic') && body?.op === 'create_message') {
        return Promise.reject(new Error('ack post failed'));
      }
      if (url.includes('heartbeat')) {
        heartbeatCount += 1;
        return Promise.resolve({ authority_held: true, stop_requested: true });
      }
      return Promise.resolve({});
    });

    const session = makeSession(() => blockedTurnPromise, () => true);
    mockStartSession.mockReturnValue(session);

    const handlerDone = handleStrategicResident(
      'task-ack-fail',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_operator_ack_threshold_seconds: 2,
      },
      { project_metadata: { project_id: 'proj-ack-fail' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    // Ack fires at 2s and its post fails (non-fatal)
    await vi.advanceTimersByTimeAsync(3000);

    // Handler must still complete normally after the ack failure
    resolveTurn(turnResult({ sentOperatorMessage: true }));
    await vi.advanceTimersByTimeAsync(1000);
    await handlerDone;

    // The heartbeat (proof the turn completed) must have been reached
    expect(heartbeatCount).toBeGreaterThanOrEqual(1);
  });
});
