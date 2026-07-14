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

/** A ResidentTurnResult with sane defaults, overridable per test. */
function turnResult(over: Partial<ResidentTurnResult> = {}): ResidentTurnResult {
  return {
    resultText: '',
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
  });

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
    let messagePollCount = 0;
    let capturedCheckpoint = '';

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
          resultText: s1Calls === 1 ? 'looked around' : 'answered the operator',
          inputTokens: 5_000,
        }));
      },
      () => true,
    );
    const session2 = makeSession(
      () => { events.push('prime'); return Promise.resolve(turnResult({ inputTokens: 0 })); },
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
    expect(autonomousPrompt).toContain('OPERATOR-DECISION CORRECTIVE');
  });

  it('re-clones the vanished workspace with a FRESH READ token, never the session-start push token', async () => {
    // WORKSPACE is stubbed to '' so `${WORKSPACE}/.git` never exists → the per-turn
    // "workspace vanished" guard fires on the first iteration. This pins the CALL
    // SITE (not just the helper): the guard must route through
    // recloneResidentWorkspace, which mints a fresh read-scoped token — not re-use
    // the push-capable `ghtok` captured at session start (expired by mid-session,
    // and a standing ambient push credential).
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

    // A single dead session so the loop primes nothing and exits after one guard pass.
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
    mockSendLog.mockReset();
    mockWriteGitTokenFile.mockReset();
    mockSetupGitCredentialHelper.mockReset();
    mockPrepareWorkspaceForRole.mockReset();
  });

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
        resultText: 'I agree, approach Y is better.\nOPERATOR-DECISION: use approach Y, not X',
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
    expect(recordOperatorDecisionCapture).toHaveBeenCalledWith(true);
  });

  it('calls recordOperatorDecisionCapture(false) and emits status when operator turn has no marker', async () => {
    const OPERATOR_MSG = {
      id: 'msg-nomarker',
      content: 'Try approach Z.',
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
        resultText: 'OK, will use approach Z.',
      })),
      () => true,
    );
    mockStartSession.mockReturnValue(session);

    mockSendWsFrame.mockReset();
    mockSendWsFrame.mockReturnValue(true);

    await handleStrategicResident(
      'task-nm', {}, { project_metadata: { project_id: 'proj-nm' } },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    expect(recordOperatorDecisionCapture).toHaveBeenCalledWith(false);
  });
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
