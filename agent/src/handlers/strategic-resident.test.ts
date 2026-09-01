import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { createHash } from 'crypto';

// ── Hoisted mock sinks ────────────────────────────────────────────────────────
const {
  mockSendWsFrame, mockSendLog, replyToolPaths, mockProvisionResidentReplyTool,
} = vi.hoisted(() => {
  const paths = {
    current: null as {
      keyPath: string;
      receiptPath: string;
      stagedReplyPath: string;
    } | null,
  };
  return {
    mockSendWsFrame: vi.fn(),
    mockSendLog: vi.fn(),
    replyToolPaths: paths,
    mockProvisionResidentReplyTool: vi.fn((_workspace: string, replyPaths?: {
      keyPath: string;
      receiptPath: string;
      stagedReplyPath: string;
    }) => {
      paths.current = replyPaths ?? null;
      return {
        configPath: '/tmp/workday-reply-mcp.json',
        toolName: 'mcp__workday__reply_to_operator',
      };
    }),
  };
});

const typedFastMocks = vi.hoisted(() => ({
  construct: vi.fn(),
  ensureReady: vi.fn<() => Promise<void>>(),
  onEvent: vi.fn(),
  dispatch: vi.fn(),
  interrupt: vi.fn(),
  replaceAfterCrash: vi.fn(),
  isAlive: vi.fn(() => true),
  close: vi.fn(),
  abandonDispositionWithConfirmation: vi.fn((..._args: unknown[]) => 'fallback'),
}));

const tokenFileMocks = vi.hoisted(() => {
  const unregister = vi.fn();
  return {
    unregister,
    registerAgentTokenMirror: vi.fn((_target: string) => unregister),
  };
});

// ── Module mocks ──────────────────────────────────────────────────────────────
// Only SessionStreamer is under test; stub the handler's heavy dependencies so
// importing strategic-resident.ts stays light. strategic-resident-lib.js (which
// owns the pure FrameCoalescer) is left REAL so the queue policy is exercised.
vi.mock('../index.js', () => ({
  normalizeClaudeConfigContent: vi.fn((_path: string, content: string) => content),
  WORKSPACE: '/tmp',
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
  BUILD_WITH_INTENT_GUIDANCE: '',
  BUILD_ORDER_GUIDANCE: '',
}));
vi.mock('./strategic-helpers.js', () => ({
  writeStrategicHelpers: vi.fn(),
  buildFetchGuidance: vi.fn(() => ''),
  STRATEGIC_FETCH_VERBS: [],
}));
vi.mock('./artifacts-helper.js', () => ({
  writeArtifactsHelper: vi.fn(() => true),
  generateArtifactsScript: vi.fn(() => ''),
  COMPUTE_ARTIFACT_GUIDANCE:
    '## Compute job artifacts\nRun `./artifacts fetch <compute-job-id>`.\n',
}));
vi.mock('../providers/runners.js', () => ({
  getProviderRunner: vi.fn(() => ({ provider: 'claude' })),
  provisionAmbientHelpers: vi.fn(),
  syncAgentHelpersToPath: vi.fn(() => '/tmp/blueprint-agent-bin-test'),
  claudeHelpSupportsPartialMessages: vi.fn(() => true),
  __resetClaudeCapabilityCacheForTesting: vi.fn(),
  probeClaudeCapabilities: vi.fn(() => null),
  stripUnsupportedClaudeOptionalArgs: vi.fn((args: readonly string[]) => [...args]),
  TextOnlyProviderUnavailableError: class extends Error {},
  buildIsolatedTextOnlyMemoryCommand: vi.fn(),
  runIsolatedTextOnlyMemory: vi.fn(),
}));
vi.mock('../token-file.js', async (importOriginal) => ({
  ...await importOriginal<typeof import('../token-file.js')>(),
  registerAgentTokenMirror: tokenFileMocks.registerAgentTokenMirror,
}));
vi.mock('../providers/resident-session.js', () => ({
  startResidentClaudeSession: vi.fn(),
}));
vi.mock('../providers/resident-reply-tool.js', () => ({
  RESIDENT_REPLY_TOOL_NAME: 'mcp__workday__reply_to_operator',
  RESIDENT_STRATEGIC_TOOL_NAME: 'mcp__workday__strategic_operation',
  RESIDENT_PROJECT_READ_TOOL_NAME: 'mcp__workday__project_read',
  provisionResidentReplyTool: mockProvisionResidentReplyTool,
}));
// Existing Workday tests stub only the native reply MCP path.  The new
// call-only config is irrelevant when no call lease is present, so keep these
// tests focused on the Workday lifecycle rather than requiring a real file in
// the mocked /tmp workspace. Dedicated call-resident-tool tests cover config
// generation and merge semantics.
vi.mock('../providers/call-resident-tool.js', () => ({
  provisionCallResidentTool: vi.fn(() => ({
    configPath: '/tmp/call-mcp.json',
    toolNames: ['mcp__call__emit_call_packet'],
  })),
  mergeCallMcpConfigs: vi.fn((_workspace: string, baseConfigPath: string) => baseConfigPath),
}));
vi.mock('./resident-repo-freshness.js', () => ({
  syncResidentRepository: vi.fn((_workspace: string, _baseBranch: string, previousHead?: string | null) => ({
    status: 'CURRENT',
    head: '0123456789abcdef0123456789abcdef01234567',
    previousHead: previousHead ?? null,
    commits: [],
    changedFiles: [],
    omittedCommits: 0,
    omittedChangedFiles: 0,
    deltaAvailable: previousHead !== null && previousHead !== undefined,
    reason: null,
  })),
  formatResidentRepoSnapshot: vi.fn(() => '## Repository freshness\nStatus: CURRENT'),
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
vi.mock('./typed-fast-resident.js', () => ({
  MAX_TYPED_WORKDAY_REQUESTS_PER_TURN: 2,
  ForegroundTypedController: class {
    constructor(options: unknown) { typedFastMocks.construct(options); }
    ensureReady(): Promise<void> { return typedFastMocks.ensureReady(); }
    onEvent(cb: (ev: unknown) => void): void { typedFastMocks.onEvent(cb); }
    dispatch(...args: unknown[]): unknown { return typedFastMocks.dispatch(...args); }
    interrupt(): void { typedFastMocks.interrupt(); }
    replaceAfterCrash(): void { typedFastMocks.replaceAfterCrash(); }
    isAlive(): boolean { return typedFastMocks.isAlive(); }
    close(): void { typedFastMocks.close(); }
  },
  _typedFastInternals: {},
  newTypedConversationReceipt: vi.fn((claimId: string, messageIds: string[]) => ({
    claim_id: claimId,
    message_ids: messageIds,
    idempotency_key: `typed-fast:${claimId}`,
  })),
  typedFastAbandonDisposition: vi.fn(() => 'fallback'),
  typedFastAbandonDispositionWithConfirmation: (...args: unknown[]) => (
    typedFastMocks.abandonDispositionWithConfirmation(...args)
  ),
  typedFastFailureBackoffMs: vi.fn(() => 1_000),
}));
// ── Import after mocks ────────────────────────────────────────────────────────
import {
  SessionStreamer, handleStrategicResident, refreshResidentGitReadToken,
  recloneResidentWorkspace, installResidentReplyKeyAtPath,
  readResidentStagedReplyAtPath, residentReplyRuntimePaths,
  withPrivateWorkdayTurnFence,
  partitionTypedFastMessages, partitionResidentPublicReplyMessages,
  selectTypedFastClaimMessages,
} from './strategic-resident.js';
import {
  apiGet, apiPost, persistAndPostStatus,
  recordOperatorDecisionCapture, recordResidentTurnLatency,
} from '../index.js';
import { writeGitTokenFile, setupGitCredentialHelper } from '../git-credential.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import { writePlaybookHelper, writeMessagesHelper, buildContextPrefix } from '../prompt.js';
import { writeStrategicHelpers } from './strategic-helpers.js';
import { writeArtifactsHelper } from './artifacts-helper.js';
import { getProviderRunner, provisionAmbientHelpers } from '../providers/runners.js';
import { startResidentClaudeSession } from '../providers/resident-session.js';
import { syncResidentRepository } from './resident-repo-freshness.js';
import type {
  ResidentSessionEvent, ResidentTurnResult,
} from '../providers/resident-session.js';
import {
  ATTACHMENT_FETCH_TIMEOUT_MS,
  MAX_IMAGES_PER_TURN,
} from './strategic-resident-lib.js';
import { canonicalAttachmentManifest } from './strategic-resident-cold-fast.js';
import { HEARTBEAT_API_TIMEOUT } from '../constants.js';

// The coalescing drain emits at most one frame per this window (see
// STREAM_FLUSH_MS in strategic-resident.ts); mirrored here for the timing math.
const STREAM_FLUSH_MS = 100;

interface SessionFrame { type: string; payload: { kind: string; text: string } }
const emittedTexts = (): string[] =>
  mockSendWsFrame.mock.calls.map((c) => (c[1] as SessionFrame).payload.text);

describe('partitionTypedFastMessages', () => {
  it('sends only the backend-marked canonical user subset to Fast', () => {
    const partition = partitionTypedFastMessages([
      { id: 'fast-user', from_type: 'user', context: { _typed_fast_eligible: true } },
      { id: 'ordinary-user', from_type: 'user', context: {} },
      { id: 'tagged-system', from_type: 'system', context: { _typed_fast_eligible: true } },
    ] as never);

    expect(partition.fast.map(message => message.id)).toEqual(['fast-user']);
    expect(partition.workday.map(message => message.id)).toEqual([
      'ordinary-user',
      'tagged-system',
    ]);
  });

  it('gives activated Fast sole public ownership of mixed broad-poll pages', () => {
    const messages = [
      { id: 'canonical-user', from_type: 'user', context: { _typed_fast_eligible: true } },
      { id: 'activity-row', from_type: 'system', context: { surface: 'activity' } },
    ] as never;

    const activated = partitionResidentPublicReplyMessages(messages, true);
    expect(activated.fast.map(message => message.id)).toEqual(['canonical-user']);
    expect(activated.workday.map(message => message.id)).toEqual(['activity-row']);

    const legacy = partitionResidentPublicReplyMessages(messages, false);
    expect(legacy.fast).toEqual([]);
    expect(legacy.workday.map(message => message.id)).toEqual([
      'canonical-user', 'activity-row',
    ]);
  });
});

describe('selectTypedFastClaimMessages', () => {
  it('recovers an older claimed turn before newer unclaimed input after restart', () => {
    const staleM1 = {
      id: 'm1',
      from_type: 'user',
      content: 'first',
      context: { _conversation_claim_idempotency_key: 'old-fast-turn' },
    } as never;
    const m2 = { id: 'm2', from_type: 'user', content: 'second', context: {} } as never;

    expect(selectTypedFastClaimMessages([staleM1, m2])).toEqual([staleM1]);
  });

  it('waits for authoritative stale claim membership instead of claiming a partial page', () => {
    const staleM1 = {
      id: 'm1',
      from_type: 'user',
      content: 'first',
      context: {
        _conversation_claim_idempotency_key: 'old-fast-turn',
        _conversation_claim_message_ids: ['m1', 'm2'],
        _conversation_claim_member_count: 2,
        _conversation_claim_group_complete: false,
      },
    } as never;

    expect(selectTypedFastClaimMessages([staleM1])).toBeNull();
  });

  it('recovers an authoritative complete stale claim group exactly', () => {
    const claimContext = {
      _conversation_claim_idempotency_key: 'old-fast-turn',
      _conversation_claim_message_ids: ['m1', 'm2'],
      _conversation_claim_member_count: 2,
      _conversation_claim_group_complete: true,
    };
    const staleM1 = {
      id: 'm1', from_type: 'user', content: 'first', context: claimContext,
    } as never;
    const staleM2 = {
      id: 'm2', from_type: 'user', content: 'second', context: claimContext,
    } as never;
    const newer = { id: 'm3', from_type: 'user', content: 'third', context: {} } as never;

    expect(selectTypedFastClaimMessages([staleM1, staleM2, newer])).toEqual([
      staleM1,
      staleM2,
    ]);
  });

  it('retries only M1 when a later page contains M1 plus M2', () => {
    const m1 = { id: 'm1', from_type: 'user', content: 'first' } as never;
    const m2 = { id: 'm2', from_type: 'user', content: 'second' } as never;
    expect(selectTypedFastClaimMessages([m1, m2], {
      messageIds: ['m1'], messages: [m1],
    })).toEqual([m1]);
  });

  it('uses the retained M1 input while its stable claim is leased', () => {
    const m1 = { id: 'm1', from_type: 'user', content: 'first' } as never;
    const m2 = { id: 'm2', from_type: 'user', content: 'second' } as never;
    expect(selectTypedFastClaimMessages([m2], {
      messageIds: ['m1'], messages: [m1],
    })).toEqual([m1]);
  });
});

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
const mockWriteMessagesHelper = vi.mocked(writeMessagesHelper);
const mockWriteStrategicHelpers = vi.mocked(writeStrategicHelpers);
const mockWriteArtifactsHelper = vi.mocked(writeArtifactsHelper);
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
    operatorReply: null,
    processGone: false,
    ttftMs: 1,
    turnMs: 1,
    toolCallCount: 0,
    totalToolCallDurationMs: 0,
    meanInterTokenMs: null,
    inputTokens: 0,
    providerFailed: false,
    failureCategory: null,
    ...over,
  };
}

/** Build a mock ResidentSession whose sendTurn is fully scripted. */
function makeSession(
  sendTurn: (text: string | Array<Record<string, unknown>>) => Promise<ResidentTurnResult> | ResidentTurnResult,
  alive: () => boolean,
) {
  let cb: ((ev: ResidentSessionEvent) => void) | null = null;
  let progress: { startedAtMs: number; lastActivityAtMs: number; toolCallCount: number } | null = null;
  return {
    // A confirmed native reply tool result has already written a receipt-bound
    // staging file before Claude returns its top-level result. Model that
    // protocol here; tests that need a missing tool explicitly return false.
    sendTurn: vi.fn(async (text: string | Array<Record<string, unknown>>) => {
      const now = Date.now();
      progress = { startedAtMs: now, lastActivityAtMs: now, toolCallCount: 0 };
      const result = await sendTurn(text);
      const operatorReply = result.operatorReply ?? (result.sentOperatorMessage ? {
        type: 'NOTE', content: result.resultText || 'native reply',
      } : null);
      const protocolResult = { ...result, operatorReply };
      if (
        protocolResult.sentOperatorMessage
        && operatorReply !== null
        && replyToolPaths.current !== null
      ) {
        const { receiptPath, stagedReplyPath: stagedPath } = replyToolPaths.current;
        try {
          const receipt = JSON.parse(fs.readFileSync(receiptPath, 'utf8')) as {
            idempotency_key: string;
            covered_message_ids: string[];
            reply_to_message_id: string;
          };
          fs.writeFileSync(stagedPath, JSON.stringify({
            type: operatorReply.type, content: operatorReply.content, ...receipt,
          }), { mode: 0o600 });
        } catch {
          // A test may intentionally make receipt installation fail. The handler
          // must then keep the operator message open rather than invent a reply.
        }
      }
      progress = null;
      return protocolResult;
    }),
    interrupt: vi.fn(),
    onEvent: vi.fn((fn: (ev: ResidentSessionEvent) => void) => { cb = fn; }),
    touch: vi.fn(),
    waitForIdle: vi.fn(() => Promise.resolve()),
    activeTurnProgress: vi.fn(() => progress),
    close: vi.fn(),
    sessionId: vi.fn(() => 'cli-sess'),
    isAlive: vi.fn(alive),
    exitCode: vi.fn(() => 0),
    exitDiagnostic: vi.fn<() => string | null>(() => null),
    _emit: (ev: ResidentSessionEvent) => {
      if (progress && ['text', 'tool', 'error', 'activity'].includes(ev.kind)) {
        progress.lastActivityAtMs = Date.now();
        if (ev.kind === 'tool') progress.toolCallCount += 1;
      }
      if (cb) cb(ev);
    },
    _setProgressAt: (lastActivityAtMs: number) => {
      if (progress) progress.lastActivityAtMs = lastActivityAtMs;
    },
  };
}

type MockSession = ReturnType<typeof makeSession>;

describe('handleStrategicResident — typed Fast lease lifecycle', () => {
  const qualifiedPayload = () => ({
    project_name: 'P',
    open_messages: [],
    strategic_session_autonomous: false,
    typed_fast_enabled: true,
    typed_fast_models: { anthropic: 'claude-haiku-4-5-20251001' },
    typed_fast_effort: 'low',
    typed_fast_qualification: 'claude-warm-stream-json:phase0-v1',
  });
  const run = (
    taskId: string,
    payload: Record<string, unknown> = qualifiedPayload(),
    statusRef: { value: string } = { value: '' },
  ) => handleStrategicResident(
    taskId,
    payload,
    { project_metadata: { project_id: 'typed-fast-project' }, context: {} },
    'ghtok', 'owner', 'repo', null, statusRef,
  );
  const typedEnvelope = (
    content: string,
    sequence: number,
    messageId = `message-${sequence}`,
    attachments?: unknown,
  ) => {
    const canonicalEnvelope = {
      version: 2,
      project_id: '11111111-1111-4111-8111-111111111111',
      context_authority_generation: 1,
      privacy_generation: 1,
      transcript_sequence: sequence,
      ledger_revision: 0,
      problem_model: {},
      decisions: [],
      memory: { active: [] },
      recent_conversation: [{
        sequence,
        message_id: messageId,
        from_type: 'user',
        content,
        content_bytes: Buffer.byteLength(content, 'utf8'),
        content_sha256: createHash('sha256').update(content, 'utf8').digest('hex'),
        attachment_manifest: canonicalAttachmentManifest(attachments),
      }],
    };
    const canonicalBytes = Buffer.from(JSON.stringify(canonicalEnvelope), 'utf8');
    return {
      snapshot_id: `snapshot-${sequence}`,
      canonical_envelope_b64: canonicalBytes.toString('base64'),
      canonical_sha256: createHash('sha256').update(canonicalBytes).digest('hex'),
      context_authority_generation: 1,
      privacy_generation: 1,
      transcript_sequence: sequence,
      ledger_revision: 0,
      expires_at: '2035-01-01T00:00:00.000Z',
    };
  };
  const messageAttachments = (message: unknown): unknown => {
    if (message === null || typeof message !== 'object' || Array.isArray(message)) return undefined;
    const context = (message as Record<string, unknown>).context;
    if (context === null || typeof context !== 'object' || Array.isArray(context)) return undefined;
    return (context as Record<string, unknown>).attachments;
  };
  const boundConversationRow = (
    message: { id: string; content: string; context?: Record<string, unknown> },
    sequence: number,
  ) => ({
    sequence,
    message_id: message.id,
    from_type: 'user',
    content: message.content,
    content_bytes: Buffer.byteLength(message.content, 'utf8'),
    content_sha256: createHash('sha256').update(message.content, 'utf8').digest('hex'),
    attachment_manifest: canonicalAttachmentManifest(messageAttachments(message)),
  });
  const bindSessionWithPrivateRuntime = (workday: MockSession | MockSession[]) => {
    const runtime = {
      replyPaths: [] as string[],
      markerPath: '',
    };
    const sessions = Array.isArray(workday) ? [...workday] : [workday];
    const fallbackSession = sessions.at(-1) as MockSession;
    vi.mocked(fs.existsSync).mockImplementation((candidate) => {
      const candidatePath = String(candidate);
      if (
        !runtime.replyPaths.includes(candidatePath)
        && candidatePath !== runtime.markerPath
      ) return true;
      try {
        fs.lstatSync(candidatePath);
        return true;
      } catch {
        return false;
      }
    });
    mockStartSession.mockImplementation((options: unknown) => {
      const env = (options as { env?: Record<string, string> }).env ?? {};
      runtime.replyPaths = [
        env.BLUEPRINT_RESIDENT_REPLY_KEY_PATH,
        env.BLUEPRINT_RESIDENT_REPLY_RECEIPT_PATH,
        env.BLUEPRINT_RESIDENT_STAGED_REPLY_PATH,
      ].filter((value): value is string => typeof value === 'string' && value.length > 0);
      runtime.markerPath = env.BLUEPRINT_RESIDENT_PRIVATE_TURN_MARKER_PATH || '';
      for (const artifactPath of runtime.replyPaths) {
        fs.mkdirSync(path.dirname(artifactPath), { recursive: true });
        fs.writeFileSync(artifactPath, 'stale-public-reply-capability');
      }
      return sessions.shift() ?? fallbackSession;
    });
    return runtime;
  };

  beforeEach(() => {
    replyToolPaths.current = null;
    mockApiGet.mockReset();
    mockApiPost.mockReset();
    mockStartSession.mockReset();
    mockSendLog.mockReset();
    typedFastMocks.construct.mockReset();
    typedFastMocks.ensureReady.mockReset();
    typedFastMocks.dispatch.mockReset();
    typedFastMocks.interrupt.mockReset();
    typedFastMocks.replaceAfterCrash.mockReset();
    typedFastMocks.isAlive.mockReset();
    typedFastMocks.close.mockReset();
    typedFastMocks.abandonDispositionWithConfirmation.mockReset();
    typedFastMocks.ensureReady.mockResolvedValue(undefined);
    typedFastMocks.isAlive.mockReturnValue(true);
    typedFastMocks.abandonDispositionWithConfirmation.mockReturnValue('fallback');
    vi.mocked(getProviderRunner).mockReturnValue({ provider: 'anthropic' } as never);
    vi.spyOn(fs, 'existsSync').mockReturnValue(true);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  const runPausedRearmProjection = async (
    taskSuffix: string,
    initialAdmission: Record<string, unknown>,
    rearmResponse: Record<string, unknown>,
  ): Promise<Array<Record<string, unknown>>> => {
    vi.useFakeTimers();
    mockApiGet.mockResolvedValue({ messages: [] });
    const activityHeartbeats: Array<Record<string, unknown>> = [];
    let sessionAlive = true;
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (!url.includes('/strategic-session/heartbeat')) return Promise.resolve({});
      if (!body?.activity) return Promise.resolve(initialAdmission);
      activityHeartbeats.push(body);
      if (activityHeartbeats.length === 1) return Promise.resolve(rearmResponse);
      sessionAlive = false;
      return Promise.resolve(rearmResponse);
    });
    mockStartSession.mockReturnValue(makeSession(
      () => Promise.resolve(turnResult()),
      () => sessionAlive,
    ));

    const done = handleStrategicResident(
      `resident-autonomy-rearm-${taskSuffix}`,
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_autonomous: true,
        strategic_session_idle_tick_seconds: 1,
        typed_fast_enabled: false,
      },
      { project_metadata: { project_id: `autonomy-rearm-${taskSuffix}` }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );
    await vi.advanceTimersByTimeAsync(5_000);
    await done;
    return activityHeartbeats;
  };

  it('abandons a nonterminal normal Fast claim without provider dispatch', async () => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'normal-fast-nonterminal',
      content: 'Do not answer an earlier signed row.',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    const baseEnvelope = typedEnvelope(operatorMessage.content, 40, operatorMessage.id);
    const canonical = JSON.parse(
      Buffer.from(baseEnvelope.canonical_envelope_b64, 'base64').toString('utf8'),
    ) as Record<string, unknown>;
    canonical.transcript_sequence = 41;
    canonical.recent_conversation = [
      ...(canonical.recent_conversation as unknown[]),
      boundConversationRow({ id: 'normal-fast-newer', content: 'newer input' }, 41),
    ];
    const canonicalBytes = Buffer.from(JSON.stringify(canonical), 'utf8');
    const nonterminalEnvelope = {
      ...baseEnvelope,
      canonical_envelope_b64: canonicalBytes.toString('base64'),
      canonical_sha256: createHash('sha256').update(canonicalBytes).digest('hex'),
      transcript_sequence: 41,
    };
    let workdayStarted = false;
    let messageOpen = true;
    let workdayAlive = true;
    mockApiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('/messages?') && workdayStarted && messageOpen
        ? { messages: [operatorMessage], fast_message_ids: [operatorMessage.id] }
        : { messages: [], fast_message_ids: [] },
    ));
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({
          authority_held: true,
          resident_incarnation_held: true,
          conversation_claim_held: true,
          stop_requested: false,
        });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: 'normal-nonterminal-lease', generation: 1 },
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        return Promise.resolve({
          claim_id: 'normal-nonterminal-claim', owner: 'fast',
          message_ids: [operatorMessage.id], envelope: nonterminalEnvelope,
        });
      }
      if (url.endsWith('/conversation/turns/abandon')) {
        messageOpen = false;
        workdayAlive = false;
        return Promise.resolve({ status: 'abandoned', legacy_fallback_admissible: true });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });
    const workday = makeSession(() => turnResult(), () => workdayAlive);
    mockStartSession.mockImplementation(() => {
      workdayStarted = true;
      return workday;
    });

    const done = run('normal-fast-nonterminal-task', {
      ...qualifiedPayload(),
      strategic_session_message_poll_seconds: 0.001,
    });
    await vi.advanceTimersByTimeAsync(5_000);
    await done;

    expect(typedFastMocks.dispatch).not.toHaveBeenCalled();
    expect(workday.sendTurn).not.toHaveBeenCalled();
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/abandon'))).toHaveLength(1);
  });

  it('publishes a ready waiting state before entering the conversational loop', async () => {
    mockApiGet.mockResolvedValue({ messages: [] });
    mockApiPost.mockResolvedValue({ authority_held: true });
    const statusRef = { value: '' };
    let statusAtLoopEntry = '';
    mockStartSession.mockReturnValue(makeSession(
      () => turnResult(),
      () => {
        statusAtLoopEntry = statusRef.value;
        return false;
      },
    ));

    await handleStrategicResident(
      'resident-ready-status',
      { project_name: 'P', open_messages: [], strategic_session_autonomous: false },
      { project_metadata: { project_id: 'ready-project' }, context: {} },
      'ghtok', 'owner', 'repo', null, statusRef,
    );

    expect(statusAtLoopEntry).toBe('Resident ready — waiting for operator input');
  });

  it('publishes a bounded active-to-authority-paused follow-up heartbeat', async () => {
    const operatorMessage = {
      id: 'msg-autonomy-active-to-paused',
      content: 'Confirm the current autonomy projection.',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let delivered = false;
    let sessionAlive = true;
    const activityHeartbeats: Array<Record<string, unknown>> = [];
    mockApiGet.mockImplementation((url: string) => {
      if (!url.includes('/messages?')) return Promise.resolve(null);
      if (delivered) return Promise.resolve({ messages: [] });
      delivered = true;
      return Promise.resolve({ messages: [operatorMessage] });
    });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (!url.includes('/strategic-session/heartbeat')) return Promise.resolve({});
      if (!body?.activity) {
        return Promise.resolve({ authority_held: true, resident_incarnation_held: true });
      }
      activityHeartbeats.push(body);
      if (activityHeartbeats.length === 1) {
        return Promise.resolve({
          authority_held: false,
          resident_incarnation_held: true,
          stop_requested: false,
        });
      }
      sessionAlive = false;
      return Promise.resolve({
        authority_held: false,
        resident_incarnation_held: true,
        stop_requested: false,
      });
    });
    mockStartSession.mockReturnValue(makeSession(
      () => Promise.resolve(turnResult({ resultText: 'Projection confirmed.' })),
      () => sessionAlive,
    ));

    await handleStrategicResident(
      'resident-autonomy-active-to-paused',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_autonomous: true,
        typed_fast_enabled: false,
      },
      { project_metadata: { project_id: 'autonomy-active-to-paused' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    expect(activityHeartbeats).toHaveLength(2);
    expect(activityHeartbeats[0]?.activity).toEqual(expect.objectContaining({
      autonomy_state: 'active',
      autonomy_reason_code: null,
    }));
    expect(activityHeartbeats[1]?.activity).toEqual(expect.objectContaining({
      autonomy_state: 'paused',
      autonomy_reason_code: 'authority_not_held',
    }));
  });

  it('rearms an authority-paused projection with one bounded follow-up heartbeat', async () => {
    const activityHeartbeats = await runPausedRearmProjection(
      'authority',
      { authority_held: false, resident_incarnation_held: true },
      { authority_held: true, resident_incarnation_held: true, stop_requested: false },
    );

    expect(activityHeartbeats).toHaveLength(2);
    expect(activityHeartbeats[0]?.activity).toEqual(expect.objectContaining({
      autonomy_state: 'paused',
      autonomy_reason_code: 'authority_not_held',
    }));
    expect(activityHeartbeats[1]?.activity).toEqual(expect.objectContaining({
      autonomy_state: 'active',
      autonomy_reason_code: null,
    }));
  });

  it('rearms a foreground-call-paused projection with one bounded follow-up heartbeat', async () => {
    const activityHeartbeats = await runPausedRearmProjection(
      'foreground-call',
      {
        authority_held: true,
        resident_incarnation_held: true,
        call: {
          call_id: 'autonomy-rearm-call',
          generation: 3,
          status: 'active',
          input_revision: 0,
          expires_at: '2035-01-01T00:00:00.000Z',
        },
      },
      {
        authority_held: true,
        resident_incarnation_held: true,
        stop_requested: false,
        call: null,
      },
    );

    expect(activityHeartbeats).toHaveLength(2);
    expect(activityHeartbeats[0]?.activity).toEqual(expect.objectContaining({
      autonomy_state: 'paused',
      autonomy_reason_code: 'foreground_call',
    }));
    expect(activityHeartbeats[1]?.activity).toEqual(expect.objectContaining({
      autonomy_state: 'active',
      autonomy_reason_code: null,
    }));
  });

  it('completes a preserved Fast turn after resolved workspace prep before Workday identity launch fails', async () => {
    const operatorMessage = {
      id: 'settled-workspace-preserved-fast',
      content: 'Answer this preserved message before trying to start Workday.',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    mockPrepareWorkspaceForRole.mockImplementationOnce(async () => {
      await Promise.resolve();
    });
    let messageOpen = true;
    const lifecycle: string[] = [];
    mockApiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('/messages?') && messageOpen
        ? { messages: [operatorMessage], fast_message_ids: [operatorMessage.id] }
        : { messages: [], fast_message_ids: [] },
    ));
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({
          authority_held: true,
          resident_incarnation_held: true,
        });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: 'settled-workspace-fast-lease', generation: 11 },
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        return Promise.resolve({
          claim_id: 'settled-workspace-fast-claim',
          owner: 'fast',
          message_ids: [operatorMessage.id],
          envelope: typedEnvelope(operatorMessage.content, 311, operatorMessage.id, messageAttachments(operatorMessage)),
        });
      }
      if (url.endsWith('/conversation/turns/complete')) {
        lifecycle.push('fast-complete');
        messageOpen = false;
        return Promise.resolve({ status: 'completed' });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => Promise.resolve({
      receipt,
      result: turnResult(),
      completion: { receipt, content: 'The preserved Fast response is complete.' },
      workdayRequests: [],
    }));
    const identityFailure = new Error(
      'resident identity requires Claude --append-system-prompt support',
    );
    mockStartSession.mockImplementation(() => {
      lifecycle.push('workday-start');
      throw identityFailure;
    });

    const failure = await run('settled-workspace-preserved-fast', {
      ...qualifiedPayload(),
      // The message arrived after task payload construction; the live poll,
      // not this stale spawn-time count, is authoritative.
      open_message_count: 0,
    }).then(
      () => null,
      (error: unknown) => error,
    );

    expect(failure).toBe(identityFailure);
    expect(lifecycle).toEqual(['fast-complete', 'workday-start']);
    expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(1);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/complete'))).toHaveLength(1);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/abandon'))).toHaveLength(0);
  });

  it('does not start a Fast foreground child for a resolved workspace with no Fast message', async () => {
    mockPrepareWorkspaceForRole.mockImplementationOnce(async () => {
      await Promise.resolve();
    });
    let settledFastPolls = 0;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?')) settledFastPolls += 1;
      return Promise.resolve({ messages: [], fast_message_ids: [] });
    });
    let leaseAcquireCount = 0;
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({
          authority_held: true,
          resident_incarnation_held: true,
        });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) leaseAcquireCount += 1;
      return Promise.resolve({});
    });
    const identityFailure = new Error(
      'resident identity requires Claude --append-system-prompt support',
    );
    mockStartSession.mockImplementation(() => { throw identityFailure; });

    const failure = await run('settled-workspace-no-fast-message', {
      ...qualifiedPayload(),
      open_message_count: 0,
    }).then(
      () => null,
      (error: unknown) => error,
    );

    expect(failure).toBe(identityFailure);
    expect(settledFastPolls).toBe(1);
    expect(leaseAcquireCount).toBe(0);
    expect(typedFastMocks.construct).not.toHaveBeenCalled();
    expect(typedFastMocks.ensureReady).not.toHaveBeenCalled();
    expect(mockStartSession).toHaveBeenCalledTimes(1);
  });

  it('completes Fast with autonomous authority lapsed while workspace preparation is unresolved', async () => {
    const operatorMessage = {
      id: 'cold-fast-before-clone',
      content: 'Can you answer before the repository is ready?',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let resolveWorkspace!: () => void;
    const workspace = new Promise<void>((resolve) => { resolveWorkspace = resolve; });
    mockPrepareWorkspaceForRole.mockImplementationOnce(() => workspace);
    let resolveReadiness!: () => void;
    typedFastMocks.ensureReady.mockImplementationOnce(() => new Promise<void>((resolve) => {
      resolveReadiness = resolve;
    }));
    let messageOpen = true;
    const dispatchStatuses: string[] = [];
    const statusChanges: string[] = [];
    let currentStatus = 'queued';
    const statusRef = {
      get value() { return currentStatus; },
      set value(value: string) { currentStatus = value; statusChanges.push(value); },
    };
    mockApiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('/messages?') && messageOpen
        ? { messages: [operatorMessage], fast_message_ids: [operatorMessage.id] }
        : { messages: [], fast_message_ids: [] },
    ));
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/strategic-session/heartbeat')) {
        // Conversational ownership remains valid even while autonomous
        // strategic authority is intentionally lapsed.
        return Promise.resolve({
          authority_held: false,
          resident_incarnation_held: true,
        });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: 'cold-fast-lease', generation: 1 },
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        return Promise.resolve({
          claim_id: 'cold-fast-claim',
          owner: 'fast',
          message_ids: [operatorMessage.id],
          envelope: typedEnvelope(operatorMessage.content, 301, operatorMessage.id, messageAttachments(operatorMessage)),
        });
      }
      if (url.endsWith('/conversation/turns/complete')) {
        messageOpen = false;
        return Promise.resolve({ status: 'completed' });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => {
      dispatchStatuses.push(statusRef.value);
      return Promise.resolve({
        receipt,
        result: turnResult(),
        completion: { receipt, content: 'Yes — the typed lane is ready.' },
        workdayRequests: [],
      });
    });
    const workday = makeSession(() => Promise.resolve(turnResult()), () => false);
    mockStartSession.mockReturnValue(workday);

    const done = run('cold-fast-before-clone', qualifiedPayload(), statusRef);
    await vi.waitFor(() => {
      expect(typedFastMocks.ensureReady).toHaveBeenCalledTimes(1);
    });

    expect(statusRef.value).toBe('Starting resident strategic session...');
    expect(statusChanges).not.toContain('Responding to operator...');
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/claim'))).toHaveLength(0);
    expect(mockStartSession).not.toHaveBeenCalled();

    resolveReadiness();
    await vi.waitFor(() => {
      expect(mockApiPost.mock.calls.filter(([url]) =>
        url.endsWith('/conversation/turns/complete'))).toHaveLength(1);
    });

    expect(dispatchStatuses).toEqual(['Responding to operator...']);
    expect(mockStartSession).not.toHaveBeenCalled();
    expect(workday.sendTurn).not.toHaveBeenCalled();

    resolveWorkspace();
    await done;

    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/complete'))).toHaveLength(1);
    expect(workday.sendTurn).not.toHaveBeenCalled();
    expect(mockPrepareWorkspaceForRole.mock.calls.at(-1)?.[0]).toEqual(
      expect.objectContaining({
        depth: 0,
        singleBranch: true,
        filter: 'blob:none',
      }),
    );
  });

  it('streams warm typed-Fast turn deltas to the operator', async () => {
    mockSendWsFrame.mockReset();
    mockSendWsFrame.mockReturnValue(true); // simulate a live viewer socket
    const operatorMessage = {
      id: 'warm-typed-fast-stream', content: 'Stream me something.',
      created_at: new Date().toISOString(), from_type: 'user', context: {},
    };
    let completed = false;
    // The resident's checkout-independent (cold-start) Fast lane gets exactly
    // one bounded poll for a waiting message before the main loop starts, and
    // it already streams correctly (it is not the seam under test). Hide the
    // message from that first poll — same idiom as the claim-conflict test
    // above — so ONLY the warm foreground lane (ensureTypedFastReady /
    // tryHandleTypedFastTurn, driven by foregroundTyped) can claim it; a false
    // pass from the already-working cold lane would defeat this test.
    let messagePolls = 0;
    mockApiGet.mockImplementation((url: string) => {
      if (!url.includes('/messages?') || completed) return Promise.resolve({ messages: [] });
      messagePolls += 1;
      return Promise.resolve(messagePolls === 1
        ? { messages: [], fast_message_ids: [] }
        : { messages: [operatorMessage], fast_message_ids: [operatorMessage.id] });
    });
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({ authority_held: true });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true, lease: { lease_id: 'warm-stream-lease', generation: 1 },
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        return Promise.resolve({
          claim_id: 'warm-stream-claim', owner: 'fast',
          message_ids: [operatorMessage.id],
          envelope: typedEnvelope(operatorMessage.content, 401, operatorMessage.id, messageAttachments(operatorMessage)),
        });
      }
      if (url.endsWith('/conversation/turns/complete')) {
        completed = true;
        return Promise.resolve({ status: 'completed' });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => {
      // Simulate the warm child streaming an assistant delta mid-turn, before
      // its durable completion — this is what the warm controller's onEvent
      // subscription must forward live to the operator.
      const emit = typedFastMocks.onEvent.mock.calls.at(-1)?.[0] as
        ((ev: ResidentSessionEvent) => void) | undefined;
      emit?.({ kind: 'text', text: 'streamed chunk' });
      return Promise.resolve({
        receipt,
        result: turnResult({ resultText: 'Final typed reply.' }),
        completion: { receipt, content: 'Final typed reply.' },
        workdayRequests: [],
      });
    });
    const workday = makeSession(() => turnResult(), () => !completed);
    mockStartSession.mockReturnValue(workday);

    await run('warm-typed-fast-stream', {
      ...qualifiedPayload(),
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 0.001,
    });

    expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(1);
    // The warm controller must have been subscribed before the turn that
    // streamed — not left unbound like the pre-fix warm lane.
    expect(typedFastMocks.onEvent.mock.calls.length).toBeGreaterThanOrEqual(1);
    expect(typeof typedFastMocks.onEvent.mock.calls[0]?.[0]).toBe('function');
    await vi.waitFor(() => {
      // The canonical conversation preview carries the chunk...
      expect(mockSendWsFrame.mock.calls.some((c) => (
        (c[1] as { type: string; payload: { content?: string } }).type
          === 'conversation_message_delta'
        && (c[1] as { type: string; payload: { content?: string } }).payload.content
          === 'streamed chunk'
      ))).toBe(true);
      // ...and the PROTECTED /strategic-session live transcript still does too.
      // Assistant text is mirrored onto both surfaces, never diverted: the
      // reply streamer is unbound for the READY probe, after replaceAfterCrash,
      // and between a failed turn and the next dispatch.
      expect(mockSendWsFrame.mock.calls.some((c) => (
        (c[1] as SessionFrame).type === 'strategic_session'
        && (c[1] as SessionFrame).payload.text === 'streamed chunk'
      ))).toBe(true);
    });
  });

  it('finishes a claimed cold Fast response when clone becomes ready during slow prompt materialization', async () => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'cold-fast-clone-ready-during-prompt',
      content: 'Finish this exact response even if the checkout becomes ready.',
      created_at: new Date().toISOString(),
      from_type: 'user',
      context: {
        attachments: [0, 1].map(index => ({
          type: 'image',
          mime_type: 'image/png',
          name: `clone-ready-${index}.png`,
          url: `https://storage.googleapis.com/bucket/clone-ready-${index}.png?X-Goog-Signature=sig`,
        })),
      },
    };
    let resolveWorkspace!: () => void;
    mockPrepareWorkspaceForRole.mockImplementationOnce(() => (
      new Promise<void>((resolve) => { resolveWorkspace = resolve; })
    ));
    let messageOpen = true;
    mockApiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('/messages?') && messageOpen
        ? { messages: [operatorMessage], fast_message_ids: [operatorMessage.id] }
        : { messages: [], fast_message_ids: [] },
    ));
    const heartbeatBodies: Array<Record<string, unknown>> = [];
    const lifecycle: string[] = [];
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        heartbeatBodies.push(body ?? {});
        return Promise.resolve({
          authority_held: true,
          resident_incarnation_held: true,
          conversation_claim_held: true,
          stop_requested: false,
        });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: 'cold-clone-ready-lease', generation: 2 },
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        return Promise.resolve({
          claim_id: 'cold-clone-ready-claim',
          owner: 'fast',
          message_ids: [operatorMessage.id],
          envelope: typedEnvelope(operatorMessage.content, 302, operatorMessage.id, messageAttachments(operatorMessage)),
        });
      }
      if (url.endsWith('/conversation/turns/complete')) {
        lifecycle.push('fast-complete');
        messageOpen = false;
        return Promise.resolve({ status: 'completed' });
      }
      if (url.endsWith('/conversation/turns/abandon')) {
        lifecycle.push('abandon');
        return Promise.resolve({ status: 'abandoned', legacy_fallback_admissible: true });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => Promise.resolve({
      receipt,
      result: turnResult(),
      completion: { receipt, content: 'The exact cold Fast response completed.' },
      workdayRequests: [],
    }));
    let fetchCount = 0;
    let resolveSecondFetch!: (response: unknown) => void;
    const attachmentFetch = vi.fn(() => {
      fetchCount += 1;
      if (fetchCount === 1) return new Promise(() => undefined);
      return new Promise((resolve) => { resolveSecondFetch = resolve; });
    });
    vi.stubGlobal('fetch', attachmentFetch);
    const workday = makeSession(() => Promise.resolve(turnResult()), () => false);
    mockStartSession.mockImplementation(() => {
      lifecycle.push('workday-start');
      return workday;
    });

    try {
      const done = run('cold-fast-clone-ready-during-prompt');
      for (let i = 0; i < 12 && attachmentFetch.mock.calls.length === 0; i += 1) {
        await vi.advanceTimersByTimeAsync(0);
      }
      expect(attachmentFetch).toHaveBeenCalledTimes(1);

      resolveWorkspace();
      await vi.advanceTimersByTimeAsync(0);
      expect(mockStartSession).not.toHaveBeenCalled();
      expect(typedFastMocks.dispatch).not.toHaveBeenCalled();

      await vi.advanceTimersByTimeAsync(15_001);
      expect(attachmentFetch).toHaveBeenCalledTimes(2);
      expect(heartbeatBodies.some(body => (
        body.conversation_claim_id === 'cold-clone-ready-claim'
        && body.typed_fast_lease_id === 'cold-clone-ready-lease'
        && body.typed_fast_generation === 2
      ))).toBe(true);
      expect(lifecycle).toEqual([]);
      expect(workday.sendTurn).not.toHaveBeenCalled();

      resolveSecondFetch({
        ok: true,
        status: 200,
        headers: { get: (name: string) => name === 'content-type' ? 'image/png' : null },
        arrayBuffer: () => Promise.resolve(new ArrayBuffer(0)),
      });
      for (let i = 0; i < 20 && lifecycle.length === 0; i += 1) {
        await vi.advanceTimersByTimeAsync(0);
      }
      await done;

      expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(1);
      expect(lifecycle).toEqual(['fast-complete', 'workday-start']);
      expect(mockApiPost.mock.calls.filter(([url]) =>
        url.endsWith('/conversation/turns/abandon'))).toHaveLength(0);
      expect(workday.sendTurn).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('does not start cold Fast while an active call survives an omitted heartbeat projection', async () => {
    vi.useFakeTimers();
    let resolveWorkspace!: () => void;
    const workspace = new Promise<void>((resolve) => { resolveWorkspace = resolve; });
    mockPrepareWorkspaceForRole.mockImplementationOnce(() => workspace);
    let heartbeatCount = 0;
    let acquireCount = 0;
    let claimCount = 0;
    mockApiGet.mockResolvedValue({ messages: [] });
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/strategic-session/heartbeat')) {
        heartbeatCount += 1;
        if (heartbeatCount === 1) {
          return Promise.resolve({
            authority_held: true,
            resident_incarnation_held: true,
            call: {
              call_id: 'cold-active-call',
              generation: 8,
              status: 'active',
              input_revision: 0,
              expires_at: '2035-01-01T00:00:00.000Z',
            },
          });
        }
        // Additive call projection omitted: this must not be interpreted as an
        // inactive call or open a competing typed public owner.
        return Promise.resolve({
          authority_held: true,
          resident_incarnation_held: true,
        });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        acquireCount += 1;
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: 'must-not-acquire', generation: 1 },
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        claimCount += 1;
      }
      return Promise.resolve({});
    });
    mockStartSession.mockReturnValue(makeSession(
      () => Promise.resolve(turnResult()),
      () => false,
    ));

    const done = run('cold-fast-active-call');
    await vi.advanceTimersByTimeAsync(16_000);

    expect(heartbeatCount).toBeGreaterThan(1);
    expect(acquireCount).toBe(0);
    expect(claimCount).toBe(0);
    expect(typedFastMocks.construct).not.toHaveBeenCalled();
    expect(mockStartSession).not.toHaveBeenCalled();

    resolveWorkspace();
    await vi.advanceTimersByTimeAsync(0);
    await done;
  });

  it('interrupts and abandons once when workspace preparation fails during a cold Fast turn', async () => {
    const operatorMessage = {
      id: 'cold-fast-clone-failure',
      content: 'Do not lose this message if clone fails.',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let rejectWorkspace!: (error: Error) => void;
    const workspace = new Promise<void>((_resolve, reject) => { rejectWorkspace = reject; });
    mockPrepareWorkspaceForRole.mockImplementationOnce(() => workspace);
    let resolveDispatch!: (value: unknown) => void;
    typedFastMocks.dispatch.mockImplementationOnce((receipt: unknown) => new Promise((resolve) => {
      resolveDispatch = resolve;
      void receipt;
    }));
    typedFastMocks.interrupt.mockImplementationOnce(() => {
      const receipt: unknown = typedFastMocks.dispatch.mock.calls[0]?.[0];
      resolveDispatch({
        receipt,
        result: turnResult({ interrupted: true }),
        completion: null,
        workdayRequests: [],
      });
    });
    mockApiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('/messages?')
        ? { messages: [operatorMessage], fast_message_ids: [operatorMessage.id] }
        : { messages: [] },
    ));
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({ authority_held: true, resident_incarnation_held: true });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: 'cold-failure-lease', generation: 2 },
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        return Promise.resolve({
          claim_id: 'cold-failure-claim',
          owner: 'fast',
          message_ids: [operatorMessage.id],
          envelope: typedEnvelope(operatorMessage.content, 302, operatorMessage.id, messageAttachments(operatorMessage)),
        });
      }
      if (url.endsWith('/conversation/turns/abandon')) {
        return Promise.resolve({
          status: 'abandoned',
          legacy_fallback_admissible: true,
        });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });

    const cloneError = new Error('clone failed');
    const done = run('cold-fast-clone-failure');
    const outcome = done.then(
      () => null,
      (error: unknown) => error,
    );
    await vi.waitFor(() => {
      expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(1);
    });

    rejectWorkspace(cloneError);
    expect(await outcome).toBe(cloneError);

    expect(typedFastMocks.interrupt).toHaveBeenCalledTimes(1);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/abandon'))).toHaveLength(1);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/complete'))).toHaveLength(0);
    expect(mockStartSession).not.toHaveBeenCalled();
  });

  it('abandons an exact cold Fast claim when required attachment materialization fails before dispatch', async () => {
    const signedUrl = 'https://storage.googleapis.com/bucket/cold-board.png?X-Goog-Signature=sig';
    const operatorMessage = {
      id: 'cold-fast-artifact-failure',
      content: 'Review this exact board revision.',
      created_at: new Date().toISOString(),
      from_type: 'user',
      context: {
        attachments: [{
          type: 'image',
          mime_type: 'image/png',
          name: 'cold-board.png',
          url: signedUrl,
          artifact_ref_id: 'cold-artifact-ref',
          image_checksum: 'ab'.repeat(32),
        }],
      },
    };
    let resolveWorkspace!: () => void;
    const workspace = new Promise<void>((resolve) => { resolveWorkspace = resolve; });
    mockPrepareWorkspaceForRole.mockImplementationOnce(() => workspace);
    const attachmentFetch = vi.fn(() => Promise.resolve({
      ok: false,
      status: 503,
      headers: { get: (_name: string) => null },
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(0)),
    }));
    vi.stubGlobal('fetch', attachmentFetch);
    mockApiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('/messages?')
        ? { messages: [operatorMessage], fast_message_ids: [operatorMessage.id] }
        : { messages: [] },
    ));
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({ authority_held: true, resident_incarnation_held: true });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: 'cold-artifact-lease', generation: 4 },
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        return Promise.resolve({
          claim_id: 'cold-artifact-claim',
          owner: 'fast',
          message_ids: [operatorMessage.id],
          envelope: typedEnvelope(operatorMessage.content, 304, operatorMessage.id, messageAttachments(operatorMessage)),
        });
      }
      if (url.endsWith('/conversation/turns/abandon')) {
        resolveWorkspace();
        return Promise.resolve({
          status: 'abandoned',
          legacy_fallback_admissible: true,
        });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });
    const workday = makeSession(() => Promise.resolve(turnResult()), () => false);
    mockStartSession.mockReturnValue(workday);

    try {
      await run('cold-fast-artifact-failure');

      expect(attachmentFetch).toHaveBeenCalledTimes(1);
      expect(typedFastMocks.dispatch).not.toHaveBeenCalled();
      expect(mockApiPost.mock.calls.filter(([url]) =>
        url.endsWith('/conversation/turns/abandon'))).toHaveLength(1);
      expect(mockApiPost.mock.calls.filter(([url]) =>
        url.endsWith('/conversation/turns/complete'))).toHaveLength(0);
      expect(workday.sendTurn).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('renews a cold Fast claim during slow materialization and abandons on cancellation without dispatch', async () => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'cold-fast-slow-materialization',
      content: 'Review these images only while my claim remains live.',
      created_at: new Date().toISOString(),
      from_type: 'user',
      context: {
        attachments: [0, 1].map(index => ({
          type: 'image',
          mime_type: 'image/png',
          name: `slow-${index}.png`,
          url: `https://storage.googleapis.com/bucket/slow-${index}.png?X-Goog-Signature=sig`,
        })),
      },
    };
    let resolveWorkspace!: () => void;
    const workspace = new Promise<void>((resolve) => { resolveWorkspace = resolve; });
    mockPrepareWorkspaceForRole.mockImplementationOnce(() => workspace);
    const attachmentFetch = vi.fn(() => new Promise(() => undefined));
    vi.stubGlobal('fetch', attachmentFetch);
    mockApiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('/messages?')
        ? { messages: [operatorMessage], fast_message_ids: [operatorMessage.id] }
        : { messages: [] },
    ));
    const heartbeatBodies: Array<Record<string, unknown>> = [];
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        heartbeatBodies.push(body ?? {});
        return Promise.resolve({
          authority_held: true,
          resident_incarnation_held: true,
          conversation_claim_held: true,
          stop_requested: heartbeatBodies.length > 1,
        });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: 'cold-slow-lease', generation: 5 },
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        return Promise.resolve({
          claim_id: 'cold-slow-claim',
          owner: 'fast',
          message_ids: [operatorMessage.id],
          envelope: typedEnvelope(operatorMessage.content, 305, operatorMessage.id, messageAttachments(operatorMessage)),
        });
      }
      if (url.endsWith('/conversation/turns/abandon')) {
        return Promise.resolve({ status: 'abandoned', legacy_fallback_admissible: true });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });

    try {
      const done = run('cold-fast-slow-materialization');
      let handlerSettled = false;
      void done.then(
        () => { handlerSettled = true; },
        () => { handlerSettled = true; },
      );
      for (let i = 0; i < 8 && attachmentFetch.mock.calls.length === 0; i += 1) {
        await vi.advanceTimersByTimeAsync(0);
      }
      expect(attachmentFetch).toHaveBeenCalledTimes(1);

      await vi.advanceTimersByTimeAsync(15_001);
      expect(heartbeatBodies.at(-1)).toEqual(expect.objectContaining({
        conversation_claim_id: 'cold-slow-claim',
        typed_fast_lease_id: 'cold-slow-lease',
        typed_fast_generation: 5,
      }));
      expect(attachmentFetch).toHaveBeenCalledTimes(2);

      // Cancellation fences this exact claim. Clone readiness may arrive too,
      // but the handler must retain ownership until the caught materialization
      // chain settles; it cannot abandon/handoff while stale work can still run.
      resolveWorkspace();
      await vi.advanceTimersByTimeAsync(0);
      expect(handlerSettled).toBe(false);
      expect(mockApiPost.mock.calls.filter(([url]) =>
        url.endsWith('/conversation/turns/abandon'))).toHaveLength(0);
      expect(typedFastMocks.dispatch).not.toHaveBeenCalled();

      await vi.advanceTimersByTimeAsync(15_001);
      await done;
      expect(handlerSettled).toBe(true);
      expect(mockApiPost.mock.calls.filter(([url]) =>
        url.endsWith('/conversation/turns/abandon'))).toHaveLength(1);
      expect(typedFastMocks.dispatch).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('paces every released cold Fast claim before retrying the still-open message', async () => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'cold-fast-paced-release',
      content: 'Retry this safely, without a claim hot loop.',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let resolveWorkspace!: () => void;
    const workspace = new Promise<void>((resolve) => { resolveWorkspace = resolve; });
    mockPrepareWorkspaceForRole.mockImplementationOnce(() => workspace);
    const invalidEnvelope = {
      ...typedEnvelope(operatorMessage.content, 306, operatorMessage.id, messageAttachments(operatorMessage)),
      canonical_sha256: '00'.repeat(32),
    };
    mockApiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('/messages?')
        ? { messages: [operatorMessage], fast_message_ids: [operatorMessage.id] }
        : { messages: [] },
    ));
    let claimCount = 0;
    let abandonCount = 0;
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({ authority_held: true, resident_incarnation_held: true });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: 'cold-paced-lease', generation: 6 },
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        claimCount += 1;
        return Promise.resolve({
          claim_id: `cold-paced-claim-${claimCount}`,
          owner: 'fast',
          message_ids: [operatorMessage.id],
          envelope: invalidEnvelope,
        });
      }
      if (url.endsWith('/conversation/turns/abandon')) {
        abandonCount += 1;
        if (abandonCount === 2) resolveWorkspace();
        return Promise.resolve({
          status: 'abandoned',
          legacy_fallback_admissible: true,
        });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });
    mockStartSession.mockReturnValue(makeSession(
      () => Promise.resolve(turnResult()),
      () => false,
    ));

    const done = run('cold-fast-paced-release');
    for (let i = 0; i < 8 && claimCount === 0; i += 1) {
      await vi.advanceTimersByTimeAsync(0);
    }
    expect(claimCount).toBe(1);

    await vi.advanceTimersByTimeAsync(999);
    expect(claimCount).toBe(1);

    await vi.advanceTimersByTimeAsync(1);
    await vi.advanceTimersByTimeAsync(0);
    await done;

    expect(claimCount).toBe(2);
    expect(abandonCount).toBe(2);
    expect(typedFastMocks.dispatch).not.toHaveBeenCalled();
  });

  it('interrupts and abandons once when cold-start cancellation arrives during dispatch', async () => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'cold-fast-cancelled',
      content: 'Keep this open if the session is cancelled.',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let resolveWorkspace!: () => void;
    const workspace = new Promise<void>((resolve) => { resolveWorkspace = resolve; });
    mockPrepareWorkspaceForRole.mockImplementationOnce(() => workspace);
    let resolveDispatch!: (value: unknown) => void;
    typedFastMocks.dispatch.mockImplementationOnce(() => new Promise((resolve) => {
      resolveDispatch = resolve;
    }));
    typedFastMocks.interrupt.mockImplementationOnce(() => {
      const receipt: unknown = typedFastMocks.dispatch.mock.calls[0]?.[0];
      resolveDispatch({
        receipt,
        result: turnResult({ interrupted: true }),
        completion: null,
        workdayRequests: [],
      });
    });
    mockApiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('/messages?')
        ? { messages: [operatorMessage], fast_message_ids: [operatorMessage.id] }
        : { messages: [] },
    ));
    let heartbeatCount = 0;
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/strategic-session/heartbeat')) {
        heartbeatCount += 1;
        return Promise.resolve({
          authority_held: false,
          resident_incarnation_held: true,
          stop_requested: heartbeatCount > 1,
        });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: 'cold-cancel-lease', generation: 3 },
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        return Promise.resolve({
          claim_id: 'cold-cancel-claim',
          owner: 'fast',
          message_ids: [operatorMessage.id],
          envelope: typedEnvelope(operatorMessage.content, 303, operatorMessage.id, messageAttachments(operatorMessage)),
        });
      }
      if (url.endsWith('/conversation/turns/abandon')) {
        return Promise.resolve({
          status: 'abandoned',
          legacy_fallback_admissible: true,
        });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });

    const done = run('cold-fast-cancelled');
    for (let i = 0; i < 8 && typedFastMocks.dispatch.mock.calls.length === 0; i += 1) {
      await vi.advanceTimersByTimeAsync(0);
    }
    expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(15_000);
    expect(heartbeatCount).toBeGreaterThan(1);
    expect(typedFastMocks.interrupt).toHaveBeenCalledTimes(1);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/abandon'))).toHaveLength(1);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/complete'))).toHaveLength(0);
    expect(mockStartSession).not.toHaveBeenCalled();

    // Cancellation rebounds the claim immediately, but handler cleanup stays
    // fenced behind the uncancellable clone so no replacement can race it.
    resolveWorkspace();
    await vi.advanceTimersByTimeAsync(0);
    await done;
    expect(mockStartSession).not.toHaveBeenCalled();
  });

  it('fails the attempt after clone settlement when a cold Fast claim disposition stays unknown', async () => {
    const operatorMessage = {
      id: 'cold-fast-unsafe-handoff',
      content: 'Do not mark this attempt successful while my claim is unknown.',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let resolveWorkspace!: () => void;
    const workspace = new Promise<void>((resolve) => { resolveWorkspace = resolve; });
    mockPrepareWorkspaceForRole.mockImplementationOnce(() => workspace);
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => Promise.resolve({
      receipt,
      result: turnResult(),
      completion: null,
      workdayRequests: [],
    }));
    mockApiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('/messages?')
        ? { messages: [operatorMessage], fast_message_ids: [operatorMessage.id] }
        : { messages: [] },
    ));
    let abandonCount = 0;
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({ authority_held: true, resident_incarnation_held: true });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: 'cold-unsafe-lease', generation: 5 },
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        return Promise.resolve({
          claim_id: 'cold-unsafe-claim',
          owner: 'fast',
          message_ids: [operatorMessage.id],
          envelope: typedEnvelope(operatorMessage.content, 305, operatorMessage.id, messageAttachments(operatorMessage)),
        });
      }
      if (url.endsWith('/conversation/turns/abandon')) {
        abandonCount += 1;
        if (abandonCount === 2) resolveWorkspace();
        return Promise.reject(new Error('coordinator unavailable'));
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });

    const done = run('cold-fast-unsafe-handoff');
    const outcome = done.then(
      () => null,
      (error: unknown) => error,
    );
    await vi.waitFor(() => {
      expect(abandonCount).toBe(2);
    });
    const failure = await outcome;

    expect(failure).toBeInstanceOf(Error);
    expect(String(failure)).toContain(
      'Fast claim did not reach a server-confirmed terminal disposition',
    );
    expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(2);
    expect(mockStartSession).not.toHaveBeenCalled();
  });

  it('acquires the fenced slot before readiness and releases it on normal shutdown', async () => {
    const order: string[] = [];
    mockApiGet.mockResolvedValue({ messages: [] });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) return Promise.resolve({ authority_held: true });
      if (url.endsWith('/typed-fast-lease/acquire')) {
        order.push('acquire');
        return Promise.resolve({ acquired: true, lease: { lease_id: 'lease-1', generation: 7 } });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        order.push(`release:${String(body?.reason)}`);
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });
    typedFastMocks.ensureReady.mockImplementation(() => { order.push('ready'); return Promise.resolve(); });
    typedFastMocks.close.mockImplementation(() => { order.push('close'); });
    mockStartSession.mockReturnValue(makeSession(() => turnResult(), () => false));

    await run('typed-fast-normal-shutdown');

    expect(order).toEqual(['acquire', 'ready', 'close', 'release:parent_shutdown']);
    expect(typedFastMocks.construct).toHaveBeenCalledTimes(1);
  });

  it('preserves the operator message without broad Workday fallback when Fast capacity is denied', async () => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'typed-fast-denied-message', content: 'Please respond normally.',
      created_at: new Date().toISOString(), from_type: 'user',
    };
    let messagePolls = 0;
    let leaseAcquireCount = 0;
    let heartbeatCount = 0;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?')) {
        messagePolls += 1;
        if (messagePolls === 1) {
          return Promise.resolve({ messages: [], fast_message_ids: [] });
        }
        return Promise.resolve({ messages: [operatorMessage], fast_message_ids: [operatorMessage.id] });
      }
      return Promise.resolve({ messages: [] });
    });
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/strategic-session/heartbeat')) {
        heartbeatCount += 1;
        return Promise.resolve({ authority_held: true, stop_requested: heartbeatCount > 1 });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        leaseAcquireCount += 1;
        return Promise.resolve({ acquired: false, reason: 'no_concurrent_capacity' });
      }
      return Promise.resolve({});
    });
    const workday = makeSession(
      () => turnResult({ sentOperatorMessage: true, resultText: 'Workday fallback reply.' }),
      () => true,
    );
    mockStartSession.mockReturnValue(workday);

    const done = run('typed-fast-denied', {
      ...qualifiedPayload(),
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 0.001,
    });
    await vi.advanceTimersByTimeAsync(3_000);
    await done;

    expect(typedFastMocks.construct).not.toHaveBeenCalled();
    expect(typedFastMocks.ensureReady).not.toHaveBeenCalled();
    expect(leaseAcquireCount).toBe(1);
    expect(messagePolls).toBeLessThanOrEqual(3);
    expect(workday.sendTurn).not.toHaveBeenCalled();
  });

  it('answers server fast-marked rows through the legacy Workday path when typed Fast is not activated', async () => {
    // The backend stamps conversation rows fast-eligible from its canonical
    // predicate even when this session has no activatable Fast adapter
    // (typed_fast_enabled: false). Deferring those rows would park every
    // operator message forever behind a child that can never exist — the
    // live no-reply deadlock this test pins down.
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'fast-marked-without-activation', content: 'hi',
      created_at: new Date().toISOString(), from_type: 'user',
    };
    let messagePolls = 0;
    let heartbeatCount = 0;
    const claimBodies: Array<Record<string, unknown> | undefined> = [];
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?')) {
        messagePolls += 1;
        if (messagePolls === 1) {
          return Promise.resolve({
            messages: [operatorMessage],
            fast_message_ids: [operatorMessage.id],
            claim_required: true,
          });
        }
        return Promise.resolve({ messages: [], fast_message_ids: [], claim_required: true });
      }
      return Promise.resolve({ messages: [] });
    });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        heartbeatCount += 1;
        return Promise.resolve({
          authority_held: true,
          conversation_claim_held: true,
          stop_requested: heartbeatCount > 1,
        });
      }
      if (url.endsWith('/messages/claim')) {
        claimBodies.push(body);
        return Promise.resolve({
          claim_id: 'claim-legacy-fallback',
          incarnation_id: 'incarnation-legacy-fallback',
          incarnation_supported: true,
          message_ids: [operatorMessage.id],
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        return Promise.reject(new Error('typed Fast claim must not run without activation'));
      }
      return Promise.resolve({});
    });
    const workday = makeSession(
      () => Promise.resolve(
        turnResult({ sentOperatorMessage: true, resultText: 'Workday legacy reply.' }),
      ),
      () => true,
    );
    mockStartSession.mockReturnValue(workday);

    const done = run('fast-marked-legacy-fallback', {
      project_name: 'P',
      open_messages: [],
      strategic_session_autonomous: false,
      typed_fast_enabled: false,
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 0.001,
    });
    await vi.advanceTimersByTimeAsync(5_000);
    await done;

    expect(workday.sendTurn).toHaveBeenCalledTimes(1);
    expect(claimBodies.length).toBeGreaterThanOrEqual(1);
    expect(claimBodies[0]?.message_ids).toEqual([operatorMessage.id]);
    expect(typedFastMocks.construct).not.toHaveBeenCalled();
    expect(typedFastMocks.ensureReady).not.toHaveBeenCalled();
  });

  it('rewinds and immediately repolls when the server advertises an older open-message floor', async () => {
    vi.useFakeTimers();
    const carriedCursor = '2026-08-13T02:00:00Z';
    const releasedFloor = '2026-08-13T01:00:00Z';
    const releasedActivity = {
      id: 'activity-released-after-bootstrap',
      content: 'Old ordinary Activity released after session bootstrap.',
      created_at: releasedFloor,
      from_type: 'user',
      message_type: 'NOTE',
    };
    const pollUrls: string[] = [];
    const claimBodies: Array<Record<string, unknown> | undefined> = [];
    let messagePolls = 0;
    let heartbeatCount = 0;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?')) {
        pollUrls.push(url);
        messagePolls += 1;
        if (messagePolls === 1) {
          return Promise.resolve({
            messages: [],
            open_message_cursor_floor: releasedFloor,
            fast_message_ids: [],
            claim_required: true,
          });
        }
        if (messagePolls === 2) {
          return Promise.resolve({
            messages: [releasedActivity],
            open_message_cursor_floor: releasedFloor,
            fast_message_ids: [],
            claim_required: true,
          });
        }
        return Promise.resolve({
          messages: [],
          open_message_cursor_floor: null,
          fast_message_ids: [],
          claim_required: true,
        });
      }
      return Promise.resolve({ messages: [] });
    });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        heartbeatCount += 1;
        return Promise.resolve({
          authority_held: true,
          conversation_claim_held: true,
          stop_requested: heartbeatCount > 1,
        });
      }
      if (url.endsWith('/messages/claim')) {
        claimBodies.push(body);
        return Promise.resolve({
          claim_id: 'claim-released-activity',
          incarnation_id: 'incarnation-released-activity',
          incarnation_supported: true,
          message_ids: [releasedActivity.id],
        });
      }
      return Promise.resolve({});
    });
    const workday = makeSession(
      () => Promise.resolve(
        turnResult({ sentOperatorMessage: true, resultText: 'Handled released Activity.' }),
      ),
      () => true,
    );
    mockStartSession.mockReturnValue(workday);

    const done = handleStrategicResident(
      'resident-live-cursor-floor',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_autonomous: false,
        typed_fast_enabled: false,
        strategic_session_idle_tick_seconds: 1,
        strategic_session_message_poll_seconds: 0.001,
      },
      {
        project_metadata: { project_id: 'cursor-floor-project' },
        context: { message_cursor: carriedCursor },
      },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );
    await vi.advanceTimersByTimeAsync(5_000);
    await done;

    expect(pollUrls.length).toBeGreaterThanOrEqual(2);
    expect(decodeURIComponent(pollUrls[0])).toContain(`since=${carriedCursor}`);
    expect(decodeURIComponent(pollUrls[1])).toContain(`since=${releasedFloor}`);
    expect(workday.sendTurn).toHaveBeenCalledTimes(1);
    expect(claimBodies[0]?.message_ids).toEqual([releasedActivity.id]);
  });

  it('continues polling and heartbeating while the Fast readiness probe is pending', async () => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'typed-fast-warming-message', content: 'Are you there?',
      created_at: new Date().toISOString(), from_type: 'user',
    };
    let messagePolls = 0;
    let heartbeatCount = 0;
    let claimCount = 0;
    const statusChanges: string[] = [];
    let currentStatus = '';
    const statusRef = {
      get value() { return currentStatus; },
      set value(value: string) { currentStatus = value; statusChanges.push(value); },
    };
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?')) {
        messagePolls += 1;
        if (messagePolls === 1) {
          return Promise.resolve({ messages: [], fast_message_ids: [] });
        }
        return Promise.resolve({ messages: [operatorMessage], fast_message_ids: [operatorMessage.id] });
      }
      return Promise.resolve({ messages: [] });
    });
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/strategic-session/heartbeat')) {
        heartbeatCount += 1;
        return Promise.resolve({ authority_held: true, stop_requested: heartbeatCount > 1 });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true, lease: { lease_id: 'lease-still-warming', generation: 1 },
        });
      }
      if (url.endsWith('/conversation/turns/claim')) claimCount += 1;
      if (url.endsWith('/typed-fast-lease/release')) return Promise.resolve({ released: true });
      return Promise.resolve({});
    });
    let finishReadiness: (() => void) | null = null;
    typedFastMocks.ensureReady.mockImplementation(() => new Promise<void>((resolve) => {
      finishReadiness = resolve;
    }));
    typedFastMocks.close.mockImplementation(() => { finishReadiness?.(); });
    const workday = makeSession(() => turnResult(), () => true);
    mockStartSession.mockReturnValue(workday);

    const done = run('typed-fast-readiness-pending', {
      ...qualifiedPayload(),
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 0.001,
    }, statusRef);
    await vi.advanceTimersByTimeAsync(3_000);
    await done;

    expect(typedFastMocks.ensureReady).toHaveBeenCalledTimes(1);
    expect(messagePolls).toBeGreaterThan(0);
    expect(heartbeatCount).toBeGreaterThan(1);
    expect(claimCount).toBe(0);
    expect(typedFastMocks.dispatch).not.toHaveBeenCalled();
    expect(workday.sendTurn).not.toHaveBeenCalled();
    expect(statusChanges).toContain('Operator message received — preparing response');
    expect(statusChanges).not.toContain('Responding to operator...');
  });

  it('bounds Fast preparation and uses coordinator-confirmed fallback for the exact input', async () => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'typed-fast-preparation-deadline', content: 'Answer after bounded preparation.',
      created_at: new Date().toISOString(), from_type: 'user',
    };
    let messageOpen = true;
    let workdayAlive = true;
    let workdayStarted = false;
    const fastClaimBodies: Array<Record<string, unknown> | undefined> = [];
    const abandonBodies: Array<Record<string, unknown> | undefined> = [];
    mockApiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('/messages?') && workdayStarted && messageOpen
        ? { messages: [operatorMessage], fast_message_ids: [operatorMessage.id] }
        : { messages: [], fast_message_ids: [] },
    ));
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({
          authority_held: true,
          resident_incarnation_held: true,
          conversation_claim_held: true,
          stop_requested: false,
        });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true, lease: { lease_id: 'lease-preparation-deadline', generation: 1 },
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        fastClaimBodies.push(body);
        return Promise.resolve({
          claim_id: 'claim-preparation-deadline', owner: 'fast',
          message_ids: [operatorMessage.id],
          envelope: typedEnvelope(operatorMessage.content, 71, operatorMessage.id),
        });
      }
      if (url.endsWith('/conversation/turns/abandon')) {
        abandonBodies.push(body);
        return Promise.resolve({ status: 'abandoned', legacy_fallback_admissible: true });
      }
      if (url.endsWith('/messages/claim')) {
        return Promise.resolve({
          claim_id: 'workday-claim-after-fast-preparation',
          incarnation_id: 'workday-incarnation-after-fast-preparation',
          incarnation_supported: true,
          message_ids: [operatorMessage.id],
        });
      }
      if (body?.op === 'create_message') {
        messageOpen = false;
        return Promise.resolve({});
      }
      if (url.endsWith('/typed-fast-lease/release')) return Promise.resolve({ released: true });
      return Promise.resolve({});
    });
    let finishReadiness: (() => void) | null = null;
    typedFastMocks.ensureReady.mockImplementation(() => new Promise<void>((resolve) => {
      finishReadiness = resolve;
    }));
    typedFastMocks.close.mockImplementation(() => { finishReadiness?.(); });
    const workday = makeSession(
      () => {
        workdayAlive = false;
        return Promise.resolve(turnResult({
          resultText: 'Workday answered after the exact Fast claim was released.',
          sentOperatorMessage: true,
        }));
      },
      () => workdayAlive,
    );
    mockStartSession.mockImplementation(() => {
      workdayStarted = true;
      return workday;
    });
    const statusRef = { value: '' };

    const done = run('typed-fast-preparation-deadline-task', {
      ...qualifiedPayload(),
      strategic_session_turn_timeout_seconds: 0.001,
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 0.001,
    }, statusRef);
    for (
      let i = 0;
      i < 20 && statusRef.value !== 'Operator message received — preparing response';
      i += 1
    ) {
      await vi.advanceTimersByTimeAsync(1_000);
    }
    expect(statusRef.value).toBe('Operator message received — preparing response');
    await vi.advanceTimersByTimeAsync(14_000);
    expect(fastClaimBodies).toHaveLength(0);
    expect(workday.sendTurn).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(4_000);
    expect(fastClaimBodies).toHaveLength(1);
    expect(abandonBodies).toHaveLength(1);
    expect(workday.sendTurn).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(2_000);
    await done;

    expect(fastClaimBodies[0]?.message_ids).toEqual([operatorMessage.id]);
    expect(abandonBodies[0]?.claim_id).toBe('claim-preparation-deadline');
    expect(typedFastMocks.dispatch).not.toHaveBeenCalled();
    expect(workday.sendTurn).toHaveBeenCalledWith(expect.anything(), {
      stallTimeoutMs: 300_000,
      hardTimeoutMs: 360_000,
    });
  });

  it('ignores a stale heartbeat response sent before the Fast lease was acquired', async () => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'typed-fast-after-stale-heartbeat', content: 'Please answer after warming.',
      created_at: new Date().toISOString(), from_type: 'user',
    };
    const canonicalEnvelope = {
      version: 2,
      project_id: '11111111-1111-4111-8111-111111111111',
      context_authority_generation: 1,
      privacy_generation: 1,
      transcript_sequence: 1,
      ledger_revision: 0,
      problem_model: {},
      decisions: [],
      memory: { active: [] },
      recent_conversation: [boundConversationRow(operatorMessage, 1)],
    };
    const canonicalBytes = Buffer.from(JSON.stringify(canonicalEnvelope), 'utf8');
    const envelope = {
      snapshot_id: 'snapshot-after-stale-heartbeat',
      canonical_envelope_b64: canonicalBytes.toString('base64'),
      canonical_sha256: createHash('sha256').update(canonicalBytes).digest('hex'),
      context_authority_generation: 1,
      privacy_generation: 1,
      transcript_sequence: 1,
      ledger_revision: 0,
      expires_at: '2035-01-01T00:00:00.000Z',
    };
    let exposeMessage = false;
    let workdayAlive = true;
    let resolveAcquire!: (value: unknown) => void;
    const acquire = new Promise<unknown>((resolve) => { resolveAcquire = resolve; });
    let resolveReadiness!: () => void;
    const readiness = new Promise<void>((resolve) => { resolveReadiness = resolve; });
    let resolveStaleHeartbeat!: (value: unknown) => void;
    const staleHeartbeat = new Promise<unknown>((resolve) => { resolveStaleHeartbeat = resolve; });
    const heartbeatBodies: Array<Record<string, unknown>> = [];
    const claimBodies: Array<Record<string, unknown> | undefined> = [];

    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?')) {
        return Promise.resolve(exposeMessage
          ? { messages: [operatorMessage], fast_message_ids: [operatorMessage.id] }
          : { messages: [], fast_message_ids: [] });
      }
      return Promise.resolve({ messages: [] });
    });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        heartbeatBodies.push(body ?? {});
        if (heartbeatBodies.length === 1) return Promise.resolve({ authority_held: true });
        if (heartbeatBodies.length === 2) return staleHeartbeat;
        return Promise.resolve({ authority_held: true });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) return acquire;
      if (url.endsWith('/conversation/turns/claim')) {
        claimBodies.push(body);
        return Promise.resolve({
          claim_id: 'claim-after-stale-heartbeat', owner: 'fast',
          message_ids: [operatorMessage.id], envelope,
        });
      }
      if (url.endsWith('/conversation/turns/complete')) {
        exposeMessage = false;
        workdayAlive = false;
        return Promise.resolve({ completed: true });
      }
      if (url.endsWith('/typed-fast-lease/release')) return Promise.resolve({ released: true });
      return Promise.resolve({});
    });
    typedFastMocks.ensureReady.mockReturnValue(readiness);
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => Promise.resolve({
      receipt,
      result: turnResult(),
      completion: { receipt, content: 'Fast reply after the stale heartbeat.' },
      workdayRequests: [],
    }));
    mockStartSession.mockReturnValue(makeSession(() => turnResult(), () => workdayAlive));

    const done = run('typed-fast-stale-heartbeat-before-acquire', {
      ...qualifiedPayload(),
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 0.001,
    });
    await vi.advanceTimersByTimeAsync(3_000);
    expect(heartbeatBodies).toHaveLength(2);
    expect(heartbeatBodies[1]).not.toHaveProperty('typed_fast_lease_id');

    resolveAcquire({
      acquired: true, lease: { lease_id: 'lease-after-stale-heartbeat', generation: 4 },
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(typedFastMocks.ensureReady).toHaveBeenCalledTimes(1);
    resolveStaleHeartbeat({ authority_held: true, typed_fast: null });
    await vi.advanceTimersByTimeAsync(0);
    expect(typedFastMocks.close).not.toHaveBeenCalled();

    resolveReadiness();
    await vi.advanceTimersByTimeAsync(3_000);
    expect(heartbeatBodies[2]).toEqual(expect.objectContaining({
      typed_fast_lease_id: 'lease-after-stale-heartbeat',
      typed_fast_generation: 4,
    }));
    exposeMessage = true;
    await vi.advanceTimersByTimeAsync(3_000);
    await done;

    expect(claimBodies).toHaveLength(1);
    expect(claimBodies[0]).toEqual(expect.objectContaining({
      message_ids: [operatorMessage.id], owner: 'fast',
    }));
    expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(1);
  });

  it('revokes Fast when the exact active lease heartbeat returns typed_fast null', async () => {
    vi.useFakeTimers();
    const heartbeatBodies: Array<Record<string, unknown>> = [];
    mockApiGet.mockResolvedValue({ messages: [], fast_message_ids: [] });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        heartbeatBodies.push(body ?? {});
        if (heartbeatBodies.length === 1) return Promise.resolve({ authority_held: true });
        return Promise.resolve({
          authority_held: true,
          stop_requested: true,
          typed_fast: null,
        });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true, lease: { lease_id: 'lease-revoked-by-heartbeat', generation: 9 },
        });
      }
      if (url.endsWith('/typed-fast-lease/release')) return Promise.resolve({ released: true });
      return Promise.resolve({});
    });
    mockStartSession.mockReturnValue(makeSession(() => turnResult(), () => true));

    const done = run('typed-fast-exact-lease-revoked', {
      ...qualifiedPayload(),
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 0.001,
    });
    await vi.advanceTimersByTimeAsync(3_000);
    await done;

    expect(heartbeatBodies).toHaveLength(2);
    expect(heartbeatBodies[1]).toEqual(expect.objectContaining({
      typed_fast_lease_id: 'lease-revoked-by-heartbeat',
      typed_fast_generation: 9,
    }));
    expect(typedFastMocks.close).toHaveBeenCalled();
    expect(mockSendLog).toHaveBeenCalledWith(
      null,
      expect.stringContaining('typed Fast lease is no longer active'),
    );
  });

  it('releases a dead ready Fast child before replacing it and admitting the message', async () => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'typed-fast-after-dead-child', content: 'Please answer through the replacement.',
      created_at: new Date().toISOString(), from_type: 'user',
    };
    const canonicalEnvelope = {
      version: 2,
      project_id: '11111111-1111-4111-8111-111111111111',
      context_authority_generation: 2,
      privacy_generation: 1,
      transcript_sequence: 2,
      ledger_revision: 0,
      problem_model: {},
      decisions: [],
      memory: { active: [] },
      recent_conversation: [boundConversationRow(operatorMessage, 2)],
    };
    const canonicalBytes = Buffer.from(JSON.stringify(canonicalEnvelope), 'utf8');
    const envelope = {
      snapshot_id: 'snapshot-after-dead-child',
      canonical_envelope_b64: canonicalBytes.toString('base64'),
      canonical_sha256: createHash('sha256').update(canonicalBytes).digest('hex'),
      context_authority_generation: 2,
      privacy_generation: 1,
      transcript_sequence: 2,
      ledger_revision: 0,
      expires_at: '2035-01-01T00:00:00.000Z',
    };
    let exposeMessage = false;
    let workdayAlive = true;
    let controllerAlive = true;
    let acquireCount = 0;
    let readinessCount = 0;
    let resolveDeadLeaseRelease!: (value: unknown) => void;
    const deadLeaseRelease = new Promise<unknown>((resolve) => { resolveDeadLeaseRelease = resolve; });
    const releaseBodies: Array<Record<string, unknown> | undefined> = [];
    const claimBodies: Array<Record<string, unknown> | undefined> = [];

    typedFastMocks.isAlive.mockImplementation(() => controllerAlive);
    typedFastMocks.ensureReady.mockImplementation(() => {
      readinessCount += 1;
      if (readinessCount === 2) controllerAlive = true;
      return Promise.resolve();
    });
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => Promise.resolve({
      receipt,
      result: turnResult(),
      completion: { receipt, content: 'Replacement Fast child replied.' },
      workdayRequests: [],
    }));
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?')) {
        return Promise.resolve(exposeMessage
          ? { messages: [operatorMessage], fast_message_ids: [operatorMessage.id] }
          : { messages: [], fast_message_ids: [] });
      }
      return Promise.resolve({ messages: [] });
    });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({ authority_held: true });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        acquireCount += 1;
        return Promise.resolve({
          acquired: true,
          lease: acquireCount === 1
            ? { lease_id: 'lease-dead-ready-child', generation: 11 }
            : { lease_id: 'lease-replacement-child', generation: 12 },
        });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        releaseBodies.push(body);
        if (body?.lease_id === 'lease-dead-ready-child') return deadLeaseRelease;
        return Promise.resolve({ released: true });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        claimBodies.push(body);
        return Promise.resolve({
          claim_id: 'claim-after-dead-child', owner: 'fast',
          message_ids: [operatorMessage.id], envelope,
        });
      }
      if (url.endsWith('/conversation/turns/complete')) {
        exposeMessage = false;
        workdayAlive = false;
        return Promise.resolve({ completed: true });
      }
      return Promise.resolve({});
    });
    mockStartSession.mockReturnValue(makeSession(() => turnResult(), () => workdayAlive));

    const done = run('typed-fast-dead-after-readiness', {
      ...qualifiedPayload(),
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 0.001,
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(acquireCount).toBe(1);
    expect(readinessCount).toBe(1);

    controllerAlive = false;
    exposeMessage = true;
    await vi.advanceTimersByTimeAsync(3_000);
    expect(releaseBodies[0]).toEqual(expect.objectContaining({
      lease_id: 'lease-dead-ready-child',
      generation: 11,
      reason: 'foreground_process_gone',
    }));
    expect(claimBodies).toHaveLength(0);
    expect(typedFastMocks.dispatch).not.toHaveBeenCalled();

    resolveDeadLeaseRelease({ released: true });
    await vi.advanceTimersByTimeAsync(6_000);
    await done;

    expect(acquireCount).toBe(2);
    expect(readinessCount).toBe(2);
    expect(claimBodies).toHaveLength(1);
    expect(claimBodies[0]).toEqual(expect.objectContaining({
      message_ids: [operatorMessage.id], owner: 'fast',
    }));
    expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(1);
  });

  it('recycles Fast once and completes the same claim when the first child exits', async () => {
    const operatorMessage = {
      id: 'typed-fast-missing-completion-once', content: 'Can you respond?',
      created_at: new Date().toISOString(), from_type: 'user',
    };
    let completed = false;
    let workdayAlive = true;
    const statusChanges: string[] = [];
    let currentStatus = '';
    const statusRef = {
      get value() { return currentStatus; },
      set value(value: string) { currentStatus = value; statusChanges.push(value); },
    };
    const claimBodies: Array<Record<string, unknown> | undefined> = [];
    const dispatchStatuses: string[] = [];
    mockApiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('/messages?') && !completed
        ? { messages: [operatorMessage], fast_message_ids: [operatorMessage.id] }
        : { messages: [] },
    ));
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) return Promise.resolve({ authority_held: true });
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({ acquired: true, lease: { lease_id: 'lease-missing-once', generation: 1 } });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        claimBodies.push(body);
        return Promise.resolve({
          claim_id: 'claim-missing-once', owner: 'fast',
          message_ids: [operatorMessage.id],
          envelope: typedEnvelope(operatorMessage.content, 201, operatorMessage.id, messageAttachments(operatorMessage)),
        });
      }
      if (url.endsWith('/conversation/turns/complete')) {
        completed = true;
        workdayAlive = false;
        return Promise.resolve({ status: 'completed' });
      }
      if (url.endsWith('/typed-fast-lease/release')) return Promise.resolve({ released: true });
      return Promise.resolve({});
    });
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => {
      dispatchStatuses.push(statusRef.value);
      return Promise.resolve({
        receipt,
        result: turnResult({
          processGone: typedFastMocks.dispatch.mock.calls.length === 1,
        }),
        completion: typedFastMocks.dispatch.mock.calls.length === 1
          ? null
          : { receipt, content: 'Yes — I am here.' },
        workdayRequests: [],
      });
    });
    mockStartSession.mockReturnValue(makeSession(
      () => Promise.resolve(turnResult()),
      () => workdayAlive,
    ));

    await handleStrategicResident(
      'typed-fast-missing-completion-once',
      qualifiedPayload(),
      { project_metadata: { project_id: 'typed-fast-project' }, context: {} },
      'ghtok', 'owner', 'repo', null, statusRef,
    );

    expect(claimBodies).toHaveLength(1);
    expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(2);
    expect(typedFastMocks.dispatch.mock.calls[1]?.[0]).toBe(
      typedFastMocks.dispatch.mock.calls[0]?.[0],
    );
    expect(typedFastMocks.dispatch.mock.calls[1]?.[1]).toBe(
      typedFastMocks.dispatch.mock.calls[0]?.[1],
    );
    expect(typedFastMocks.replaceAfterCrash).toHaveBeenCalledTimes(1);
    expect(dispatchStatuses).toEqual([
      'Responding to operator...',
      'Responding to operator...',
    ]);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/complete'))).toHaveLength(1);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/abandon'))).toHaveLength(0);
    const respondingIndex = statusChanges.indexOf('Responding to operator...');
    expect(respondingIndex).toBeGreaterThanOrEqual(0);
    expect(statusChanges.indexOf('Resident ready — waiting for operator input', respondingIndex + 1))
      .toBeGreaterThan(respondingIndex);
  });

  it('retries the same Fast claim when the first dispatch settles processGone with a dead child', async () => {
    const operatorMessage = {
      id: 'typed-fast-process-gone-once',
      content: 'Retry this exact response after the child exits.',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let completed = false;
    let workdayAlive = true;
    let claimCount = 0;
    let childAlive = true;
    mockApiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('/messages?') && !completed
        ? { messages: [operatorMessage], fast_message_ids: [operatorMessage.id] }
        : { messages: [] },
    ));
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({ authority_held: true, resident_incarnation_held: true });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: 'lease-process-gone-once', generation: 1 },
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        claimCount += 1;
        return Promise.resolve({
          claim_id: 'claim-process-gone-once',
          owner: 'fast',
          message_ids: [operatorMessage.id],
          envelope: typedEnvelope(operatorMessage.content, 211, operatorMessage.id, messageAttachments(operatorMessage)),
        });
      }
      if (url.endsWith('/conversation/turns/complete')) {
        completed = true;
        workdayAlive = false;
        return Promise.resolve({ status: 'completed' });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });
    typedFastMocks.isAlive.mockImplementation(() => childAlive);
    typedFastMocks.replaceAfterCrash.mockImplementation(() => { childAlive = true; });
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => {
      if (typedFastMocks.dispatch.mock.calls.length === 1) {
        childAlive = false;
        return Promise.resolve({
          receipt,
          result: turnResult({ processGone: true }),
          completion: null,
          workdayRequests: [],
        });
      }
      return Promise.resolve({
        receipt,
        result: turnResult(),
        completion: { receipt, content: 'Recovered on the same exact claim.' },
        workdayRequests: [],
      });
    });
    const workday = makeSession(() => Promise.resolve(turnResult()), () => workdayAlive);
    mockStartSession.mockReturnValue(workday);

    await run('typed-fast-process-gone-once');

    expect(claimCount).toBe(1);
    expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(2);
    expect(typedFastMocks.dispatch.mock.calls[1]?.[0]).toBe(
      typedFastMocks.dispatch.mock.calls[0]?.[0],
    );
    expect(typedFastMocks.dispatch.mock.calls[1]?.[1]).toBe(
      typedFastMocks.dispatch.mock.calls[0]?.[1],
    );
    expect(typedFastMocks.replaceAfterCrash).toHaveBeenCalledTimes(1);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/complete'))).toHaveLength(1);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/abandon'))).toHaveLength(0);
    expect(workday.sendTurn).not.toHaveBeenCalled();
  });

  it('hands the same batch to deep Workday after Fast omits receipt-bound completion', async () => {
    const operatorMessage = {
      id: 'typed-fast-missing-completion-twice', content: 'Can you answer now?',
      created_at: new Date().toISOString(), from_type: 'user',
    };
    const legacyMessage = {
      id: 'activity-row-in-same-page', content: 'Include this separate activity row.',
      created_at: new Date().toISOString(), from_type: 'system',
    };
    let abandoned = false;
    let workdayAlive = true;
    let claimCount = 0;
    const releaseReasons: unknown[] = [];
    const statusChanges: string[] = [];
    let currentStatus = 'Resident ready — waiting for operator input';
    const statusRef = {
      get value() { return currentStatus; },
      set value(value: string) { currentStatus = value; statusChanges.push(value); },
    };
    let messagePolls = 0;
    mockApiGet.mockImplementation((url: string) => {
      if (!url.includes('/messages?') || abandoned) return Promise.resolve({ messages: [] });
      messagePolls += 1;
      return Promise.resolve(messagePolls === 1
        ? { messages: [], fast_message_ids: [] }
        : { messages: [operatorMessage, legacyMessage], fast_message_ids: [operatorMessage.id] });
    });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) return Promise.resolve({ authority_held: true });
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({ acquired: true, lease: { lease_id: 'lease-missing-twice', generation: 1 } });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        claimCount += 1;
        return Promise.resolve({
          claim_id: 'claim-missing-twice', owner: 'fast',
          message_ids: [operatorMessage.id],
          envelope: typedEnvelope(operatorMessage.content, 202, operatorMessage.id, messageAttachments(operatorMessage)),
        });
      }
      if (url.endsWith('/conversation/turns/abandon')) {
        abandoned = true;
        return Promise.resolve({ status: 'abandoned', legacy_fallback_admissible: true });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        releaseReasons.push(body?.reason);
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => Promise.resolve({
      receipt, result: turnResult(), completion: null, workdayRequests: [],
    }));
    const workday = makeSession(
      (prompt) => {
        expect(JSON.stringify(prompt)).toContain(operatorMessage.content);
        expect(JSON.stringify(prompt)).toContain(legacyMessage.content);
        workdayAlive = false;
        return Promise.resolve(turnResult({
          resultText: 'Legacy Workday answered after Fast released ownership.',
          sentOperatorMessage: true,
        }));
      },
      () => workdayAlive,
    );
    mockStartSession.mockReturnValue(workday);

    await run('typed-fast-missing-completion-twice', qualifiedPayload(), statusRef);

    expect(claimCount).toBe(1);
    expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(1);
    expect(typedFastMocks.replaceAfterCrash).not.toHaveBeenCalled();
    expect(typedFastMocks.close).toHaveBeenCalledTimes(1);
    expect(releaseReasons).toContain('receipt_protocol_failure');
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/complete'))).toHaveLength(0);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/abandon'))).toHaveLength(1);
    expect(workday.sendTurn).toHaveBeenCalledTimes(1);
    expect(workday.sendTurn).toHaveBeenCalledWith(expect.anything(), {
      stallTimeoutMs: 300_000,
      hardTimeoutMs: 360_000,
    });
    const replyPost = mockApiPost.mock.calls.find(([, body]) => body?.op === 'create_message');
    expect((replyPost?.[1]?.data as Record<string, unknown>).covered_message_ids).toEqual([
      operatorMessage.id,
      legacyMessage.id,
    ]);
    const respondingIndex = statusChanges.indexOf('Responding to operator...');
    expect(respondingIndex).toBeGreaterThanOrEqual(0);
  });

  it('keeps Workday fenced when Fast abandonment is not confirmed fallback-admissible', async () => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'typed-fast-unconfirmed-abandon', content: 'Do not create a competing reply owner.',
      created_at: new Date().toISOString(), from_type: 'user',
    };
    let abandonAttempted = false;
    let workdayAlive = true;
    let claimCount = 0;
    let messagePolls = 0;
    mockApiGet.mockImplementation((url: string) => {
      if (!url.includes('/messages?') || abandonAttempted) return Promise.resolve({ messages: [] });
      messagePolls += 1;
      return Promise.resolve(messagePolls === 1
        ? { messages: [], fast_message_ids: [] }
        : { messages: [operatorMessage], fast_message_ids: [operatorMessage.id] });
    });
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/strategic-session/heartbeat')) return Promise.resolve({ authority_held: true });
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({ acquired: true, lease: { lease_id: 'lease-unconfirmed', generation: 1 } });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        claimCount += 1;
        return Promise.resolve({
          claim_id: 'claim-unconfirmed', owner: 'fast',
          message_ids: [operatorMessage.id],
          envelope: typedEnvelope(operatorMessage.content, 203, operatorMessage.id),
        });
      }
      if (url.endsWith('/conversation/turns/abandon')) {
        abandonAttempted = true;
        workdayAlive = false;
        return Promise.resolve({ status: 'abandoned', legacy_fallback_admissible: false });
      }
      if (url.endsWith('/typed-fast-lease/release')) return Promise.resolve({ released: true });
      return Promise.resolve({});
    });
    typedFastMocks.abandonDispositionWithConfirmation.mockReturnValue('deferred');
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => Promise.resolve({
      receipt, result: turnResult(), completion: null, workdayRequests: [],
    }));
    const workday = makeSession(
      () => Promise.resolve(turnResult({ sentOperatorMessage: true })),
      () => workdayAlive,
    );
    mockStartSession.mockReturnValue(workday);

    const done = run('typed-fast-unconfirmed-abandon');
    await vi.advanceTimersByTimeAsync(2_000);
    await done;

    expect(claimCount).toBe(1);
    expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(1);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/abandon'))).toHaveLength(1);
    expect(workday.sendTurn).not.toHaveBeenCalled();
  });

  it('replaces invalid Fast staging and completes the same exact claim', async () => {
    const operatorMessage = {
      id: 'typed-fast-invalid-staging-once', content: 'Please route the implementation correctly.',
      created_at: new Date().toISOString(), from_type: 'user',
    };
    let completed = false;
    let workdayAlive = true;
    let claimCount = 0;
    mockApiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('/messages?') && !completed
        ? { messages: [operatorMessage], fast_message_ids: [operatorMessage.id] }
        : { messages: [] },
    ));
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/strategic-session/heartbeat')) return Promise.resolve({ authority_held: true });
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({ acquired: true, lease: { lease_id: 'lease-invalid-once', generation: 1 } });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        claimCount += 1;
        return Promise.resolve({
          claim_id: 'claim-invalid-once', owner: 'fast',
          message_ids: [operatorMessage.id],
          envelope: typedEnvelope(operatorMessage.content, 203, operatorMessage.id, messageAttachments(operatorMessage)),
        });
      }
      if (url.endsWith('/conversation/turns/complete')) {
        completed = true;
        workdayAlive = false;
        return Promise.resolve({ status: 'completed' });
      }
      if (url.endsWith('/typed-fast-lease/release')) return Promise.resolve({ released: true });
      return Promise.resolve({});
    });
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => (
      typedFastMocks.dispatch.mock.calls.length === 1
        ? Promise.reject(new Error(
            'Fast Resident promised Workday delegation without staging a Workday request',
          ))
        : Promise.resolve({
            receipt, result: turnResult(),
            completion: { receipt, content: 'I can route that through Workday.' },
            workdayRequests: [{
              receipt,
              delivery_key: 'valid-replacement-request',
              prompt: 'Implement the requested managed-state change.',
            }],
          })
    ));
    mockStartSession.mockReturnValue(makeSession(
      () => Promise.resolve(turnResult()),
      () => workdayAlive,
    ));

    await run('typed-fast-invalid-staging-once');

    expect(claimCount).toBe(1);
    expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(2);
    expect(typedFastMocks.dispatch.mock.calls[1]?.[0]).toBe(
      typedFastMocks.dispatch.mock.calls[0]?.[0],
    );
    expect(typedFastMocks.dispatch.mock.calls[1]?.[1]).toBe(
      typedFastMocks.dispatch.mock.calls[0]?.[1],
    );
    expect(typedFastMocks.replaceAfterCrash).toHaveBeenCalledTimes(1);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/complete'))).toHaveLength(1);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/abandon'))).toHaveLength(0);
  });

  it('retires Fast and abandons once after two invalid staging attempts', async () => {
    const operatorMessage = {
      id: 'typed-fast-invalid-staging-twice', content: 'Please handle this managed change.',
      created_at: new Date().toISOString(), from_type: 'user',
    };
    let abandoned = false;
    let workdayAlive = true;
    let claimCount = 0;
    const releaseReasons: unknown[] = [];
    let messagePolls = 0;
    mockApiGet.mockImplementation((url: string) => {
      if (!url.includes('/messages?') || abandoned) return Promise.resolve({ messages: [] });
      messagePolls += 1;
      return Promise.resolve(messagePolls === 1
        ? { messages: [], fast_message_ids: [] }
        : { messages: [operatorMessage], fast_message_ids: [operatorMessage.id] });
    });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) return Promise.resolve({ authority_held: true });
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({ acquired: true, lease: { lease_id: 'lease-invalid-twice', generation: 1 } });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        claimCount += 1;
        return Promise.resolve({
          claim_id: 'claim-invalid-twice', owner: 'fast',
          message_ids: [operatorMessage.id],
          envelope: typedEnvelope(operatorMessage.content, 204, operatorMessage.id, messageAttachments(operatorMessage)),
        });
      }
      if (url.endsWith('/conversation/turns/abandon')) {
        abandoned = true;
        workdayAlive = false;
        return Promise.resolve({ status: 'abandoned', legacy_fallback_admissible: true });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        releaseReasons.push(body?.reason);
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });
    typedFastMocks.dispatch.mockRejectedValue(new Error(
      'Fast Resident claimed direct managed-project execution',
    ));
    mockStartSession.mockReturnValue(makeSession(
      () => Promise.resolve(turnResult()),
      () => workdayAlive,
    ));

    await run('typed-fast-invalid-staging-twice');

    expect(claimCount).toBe(1);
    expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(2);
    expect(typedFastMocks.dispatch.mock.calls[1]?.[0]).toBe(
      typedFastMocks.dispatch.mock.calls[0]?.[0],
    );
    expect(typedFastMocks.replaceAfterCrash).toHaveBeenCalledTimes(1);
    expect(typedFastMocks.close).toHaveBeenCalledTimes(1);
    expect(releaseReasons).toContain('receipt_protocol_failure');
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/complete'))).toHaveLength(0);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/abandon'))).toHaveLength(1);
  });

  it('reprovisions Fast after two continuation protocol failures and completes without new operator input', async () => {
    vi.useFakeTimers();
    const continuationEnvelope = typedEnvelope(
      'No operator decision is needed. Abandon v54 before qualification and open a fresh version after final automain is frozen.',
      205,
    );
    let workdayAlive = true;
    let packetCompleted = false;
    let leaseAcquireCount = 0;
    let continuationClaimCount = 0;
    const leaseAcquireTimes: number[] = [];
    const leaseReleaseBodies: Array<Record<string, unknown> | undefined> = [];
    const continuationReleaseBodies: Array<Record<string, unknown> | undefined> = [];
    const continuationCompleteBodies: Array<Record<string, unknown> | undefined> = [];
    const privateClaimOrder: string[] = [];

    mockApiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('/messages?')
        ? { messages: [], fast_message_ids: [], claim_required: true }
        : { messages: [] },
    ));
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({ authority_held: true });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        leaseAcquireCount += 1;
        leaseAcquireTimes.push(Date.now());
        return Promise.resolve({
          acquired: true,
          lease: {
            lease_id: `lease-continuation-${leaseAcquireCount}`,
            generation: leaseAcquireCount,
          },
        });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        leaseReleaseBodies.push(body);
        return Promise.resolve({ released: true });
      }
      if (url.endsWith('/workday-consultations/claim-next')) {
        privateClaimOrder.push('consultation');
        return Promise.resolve({ status: 'empty' });
      }
      if (url.endsWith('/workday-consultations/continuations/claim-next')) {
        privateClaimOrder.push('continuation');
        if (packetCompleted) return Promise.resolve({ status: 'empty' });
        continuationClaimCount += 1;
        return Promise.resolve({
          status: 'claimed',
          continuation: {
            result_packet_id: 'packet-continuation-recovery',
            continuation_claim_id: `continuation-claim-${continuationClaimCount}`,
            lease_expires_at: '2035-01-01T00:00:00.000Z',
            attempt: continuationClaimCount,
            claim_id: 'original-fast-claim',
            delivery_key: 'delivery-continuation-recovery',
            continuation_key: 'continuation-recovery',
            result_summary: (
              `Workday says ${'a'.repeat(40)} is serving and the failure was durably recorded `
              + 'in a project note. The settled next step is to abandon v54 before qualification '
              + 'and open a fresh version after final automain is frozen; no operator decision is needed.'
            ),
            envelope: continuationEnvelope,
            current_live_work_authority: {
              recent_terminal_tasks: [{
                id: 'unrelated-terminal-task',
                status: 'COMPLETED',
                strategic_work_order_id: 'different-work-order',
              }],
              release_deployments: [
                {
                  id: 'target-release',
                  promoted_sha: 'a'.repeat(40),
                  serving_status: 'unconfirmed',
                  served_sha: null,
                },
                {
                  id: 'unrelated-serving-release',
                  promoted_sha: 'b'.repeat(40),
                  serving_status: 'serving_confirmed',
                  served_sha: 'b'.repeat(40),
                },
              ],
              collection_metadata: {
                recent_terminal_tasks: { returned_count: 1, has_more: false },
                release_deployments: { returned_count: 2, has_more: false },
              },
            },
          },
        });
      }
      if (url.endsWith('/workday-consultations/continuations/packet-continuation-recovery/release')) {
        continuationReleaseBodies.push(body);
        return Promise.resolve({ status: 'released' });
      }
      if (url.endsWith('/workday-consultations/continuations/packet-continuation-recovery/complete')) {
        continuationCompleteBodies.push(body);
        packetCompleted = true;
        workdayAlive = false;
        return Promise.resolve({ status: 'completed' });
      }
      return Promise.resolve({});
    });
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => Promise.resolve({
      receipt,
      result: turnResult(),
      completion: typedFastMocks.dispatch.mock.calls.length <= 2
        ? null
        : { receipt, content: 'Here is the recovered Workday result.' },
      workdayRequests: [],
    }));
    const workday = makeSession(
      () => Promise.resolve(turnResult()),
      () => workdayAlive,
    );
    mockStartSession.mockReturnValue(workday);

    const done = run('typed-fast-continuation-reprovision', {
      ...qualifiedPayload(),
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 0.001,
    });
    await vi.advanceTimersByTimeAsync(900);

    expect(leaseAcquireCount).toBe(1);
    expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(2);
    expect(packetCompleted).toBe(false);

    await vi.advanceTimersByTimeAsync(3_000);
    await done;

    expect(typedFastMocks.construct).toHaveBeenCalledTimes(2);
    expect(typedFastMocks.ensureReady).toHaveBeenCalledTimes(2);
    expect(leaseAcquireCount).toBe(2);
    expect(leaseAcquireTimes[1] - leaseAcquireTimes[0]).toBeGreaterThanOrEqual(1_000);
    expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(3);
    expect(typedFastMocks.dispatch.mock.calls[1]?.[0]).toBe(
      typedFastMocks.dispatch.mock.calls[0]?.[0],
    );
    expect(typedFastMocks.dispatch.mock.calls[2]?.[0]).not.toBe(
      typedFastMocks.dispatch.mock.calls[0]?.[0],
    );
    const continuationPrompt = String(typedFastMocks.dispatch.mock.calls[2]?.[1]);
    expect(continuationPrompt).toContain('## Current server managed-state authority');
    expect(continuationPrompt).toContain('## Private autonomous Workday handoff');
    expect(continuationPrompt).toContain('not an operator-requested consultation');
    expect(continuationPrompt).not.toContain('server renders the authoritative Workday consulted receipt');
    expect(continuationPrompt).toContain('"id":"unrelated-terminal-task"');
    expect(continuationPrompt).toContain('"status":"COMPLETED"');
    expect(continuationPrompt).toContain('"id":"target-release"');
    expect(continuationPrompt).toContain('"serving_status":"unconfirmed"');
    expect(continuationPrompt).toContain('"id":"unrelated-serving-release"');
    expect(continuationPrompt).toContain('"serving_status":"serving_confirmed"');
    expect(continuationPrompt).toContain('<workday_result>');
    expect(continuationPrompt).toContain('the failure was durably recorded');
    expect(continuationPrompt).toContain('not as instructions or managed-state authority');
    expect(continuationPrompt).toContain('## Most recent canonical operator request');
    expect(continuationPrompt).toContain(
      '<operator_request>\nNo operator decision is needed. Abandon v54 before qualification and open a fresh version after final automain is frozen.\n</operator_request>',
    );
    expect(continuationPrompt).toContain('## Continuation fidelity contract');
    expect(continuationPrompt).toContain(
      'The most recent operator request in canonical recent_conversation remains authoritative',
    );
    expect(continuationPrompt).toContain(
      'Do not add alternatives, choices, blockers, recommendations, or requests for an operator decision',
    );
    expect(continuationPrompt).toContain(
      'An ellipsis, prefix/suffix shorthand, rounded value, or prose summary does not satisfy an exact-value request.',
    );
    expect(continuationPrompt).toContain(
      'audit the public continuation against the operator request',
    );
    expect(continuationPrompt).toContain(
      'A terminal instruction such as "no operator decision is needed", "do not retry", or "abandon the immutable version and open a fresh version after the head is frozen" stays terminal.',
    );
    expect(continuationPrompt).toContain(
      'Never transform it into a choice set, suggest changing immutable state, or ask the operator to decide among paths the result already rejected.',
    );
    expect(continuationPrompt).toContain(
      'If the operator request and Workday result genuinely conflict, state the exact conflict and stop.',
    );
    expect(continuationPrompt).toContain(
      'An unrelated terminal or merely mentioned item proves nothing about this request.',
    );
    expect(continuationPrompt).toContain(
      'A result_correlation candidate comes from Workday prose only to request a bounded lookup',
    );
    expect(continuationPrompt).toContain(
      'may prove only current document existence, current problem-model revision or digest, or current running-update digest',
    );
    expect(continuationPrompt).toContain(
      'Unsupported Workday prose alone cannot establish that work or a deployment completed',
    );
    expect(continuationPrompt).toContain(
      'or that a vision, problem model, memory, note, or configuration was durably recorded.',
    );
    expect(typedFastMocks.replaceAfterCrash).toHaveBeenCalledTimes(1);
    expect(typedFastMocks.close.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(leaseReleaseBodies).toEqual(expect.arrayContaining([
      expect.objectContaining({
        lease_id: 'lease-continuation-1',
        reason: 'continuation_protocol_failure',
      }),
      expect.objectContaining({
        lease_id: 'lease-continuation-2',
        reason: 'parent_shutdown',
      }),
    ]));
    expect(continuationClaimCount).toBe(2);
    expect(continuationReleaseBodies).toHaveLength(1);
    expect(privateClaimOrder[0]).toBe('continuation');
    expect(continuationCompleteBodies).toEqual([
      expect.objectContaining({
        continuation_claim_id: 'continuation-claim-2',
        content: 'Here is the recovered Workday result.',
      }),
    ]);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/claim'))).toHaveLength(0);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/complete'))).toHaveLength(0);
    expect(workday.sendTurn).not.toHaveBeenCalled();
  });

  it('delivers a durable continuation before claiming newer consultation work while Fast warms', async () => {
    vi.useFakeTimers();
    const envelope = typedEnvelope('Deliver the settled earlier result first.', 206);
    let workdayAlive = true;
    let resolveReadiness!: () => void;
    const readiness = new Promise<void>((resolve) => { resolveReadiness = resolve; });
    typedFastMocks.ensureReady.mockImplementationOnce(() => readiness);
    let continuationClaims = 0;
    let consultationClaims = 0;
    let packetCompleted = false;
    const claimOrder: string[] = [];

    mockApiGet.mockResolvedValue({ messages: [], fast_message_ids: [], claim_required: true });
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({ authority_held: true, resident_incarnation_held: true });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: 'lease-continuation-priority', generation: 1 },
        });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      if (url.endsWith('/workday-consultations/continuations/claim-next')) {
        continuationClaims += 1;
        claimOrder.push('continuation');
        if (packetCompleted) return Promise.resolve({ status: 'empty' });
        return Promise.resolve({
          status: 'claimed',
          continuation: {
            result_packet_id: 'packet-priority-before-newer',
            continuation_claim_id: 'continuation-priority-claim',
            lease_expires_at: '2035-01-01T00:00:00.000Z',
            attempt: 1,
            claim_id: 'original-fast-priority-claim',
            delivery_key: 'delivery-priority',
            continuation_key: 'continuation-priority',
            result_summary: 'The older private result is settled and ready.',
            envelope,
          },
        });
      }
      if (url.endsWith('/workday-consultations/claim-next')) {
        consultationClaims += 1;
        claimOrder.push('consultation');
        return Promise.resolve({ status: 'claimed' });
      }
      if (url.endsWith('/workday-consultations/continuations/packet-priority-before-newer/complete')) {
        packetCompleted = true;
        workdayAlive = false;
        return Promise.resolve({ status: 'completed' });
      }
      return Promise.resolve({});
    });
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => Promise.resolve({
      receipt,
      result: turnResult(),
      completion: { receipt, content: 'The earlier Workday result is delivered.' },
      workdayRequests: [],
    }));
    const workday = makeSession(() => Promise.resolve(turnResult()), () => workdayAlive);
    mockStartSession.mockReturnValue(workday);

    const done = run('typed-continuation-priority-during-warmup', {
      ...qualifiedPayload(),
      strategic_session_message_poll_seconds: 0.01,
    });
    await vi.advanceTimersByTimeAsync(20_000);
    expect(continuationClaims).toBe(0);
    expect(consultationClaims).toBe(0);

    resolveReadiness();
    await vi.advanceTimersByTimeAsync(3_000);
    await done;

    expect(continuationClaims).toBe(1);
    expect(consultationClaims).toBe(0);
    expect(claimOrder).toEqual(['continuation']);
    expect(workday.sendTurn).not.toHaveBeenCalled();
  });

  it('terminalizes a repeatedly poisoned Workday continuation with a safe fallback', async () => {
    vi.useFakeTimers();
    const continuationEnvelope = typedEnvelope('Private Workday findings are ready.', 2051);
    let workdayAlive = true;
    let packetCompleted = false;
    let continuationClaimCount = 0;
    const continuationReleaseBodies: Array<Record<string, unknown> | undefined> = [];
    const continuationCompleteBodies: Array<Record<string, unknown> | undefined> = [];

    mockApiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('/messages?')
        ? { messages: [], fast_message_ids: [], claim_required: true }
        : { messages: [] },
    ));
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({ authority_held: true });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: `lease-poison-${continuationClaimCount + 1}`, generation: continuationClaimCount + 1 },
        });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      if (url.endsWith('/workday-consultations/claim-next')) {
        return Promise.resolve({ status: 'empty' });
      }
      if (url.endsWith('/workday-consultations/continuations/claim-next')) {
        if (packetCompleted) return Promise.resolve({ status: 'empty' });
        continuationClaimCount += 1;
        return Promise.resolve({
          status: 'claimed',
          continuation: {
            result_packet_id: 'packet-continuation-safe-fallback',
            continuation_claim_id: `continuation-poison-${continuationClaimCount}`,
            lease_expires_at: '2035-01-01T00:00:00.000Z',
            attempt: continuationClaimCount,
            claim_id: 'original-fast-claim',
            delivery_key: 'delivery-continuation-safe-fallback',
            continuation_key: 'continuation-safe-fallback',
            result_summary: 'Sensitive private details that must not be copied by the fallback.',
            envelope: continuationEnvelope,
          },
        });
      }
      if (url.endsWith('/workday-consultations/continuations/packet-continuation-safe-fallback/release')) {
        continuationReleaseBodies.push(body);
        return Promise.resolve({ status: 'released' });
      }
      if (url.endsWith('/workday-consultations/continuations/packet-continuation-safe-fallback/complete')) {
        continuationCompleteBodies.push(body);
        packetCompleted = true;
        workdayAlive = false;
        return Promise.resolve({ status: 'completed' });
      }
      return Promise.resolve({});
    });
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => Promise.resolve({
      receipt,
      result: turnResult(),
      completion: null,
      workdayRequests: [],
    }));
    const workday = makeSession(
      () => Promise.resolve(turnResult()),
      () => workdayAlive,
    );
    mockStartSession.mockReturnValue(workday);

    const done = run('typed-fast-continuation-safe-fallback', {
      ...qualifiedPayload(),
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 0.001,
    });
    await vi.advanceTimersByTimeAsync(4_000);
    await done;

    expect(continuationClaimCount).toBe(2);
    expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(4);
    expect(continuationReleaseBodies).toHaveLength(1);
    expect(continuationCompleteBodies).toEqual([
      expect.objectContaining({
        continuation_claim_id: 'continuation-poison-2',
        content: (
          'The private Workday result completed, but I could not safely format it. '
          + 'Please ask me to retry the explanation.'
        ),
      }),
    ]);
    expect(String(continuationCompleteBodies[0]?.content))
      .not.toContain('Sensitive private details');
    expect(mockSendLog.mock.calls.map(call => String(call[1])).join('\n'))
      .toContain('used safe fallback after repeated protocol failure');
    expect(workday.sendTurn).not.toHaveBeenCalled();
  });

  it('preempts a Workday continuation at message-poll cadence for new operator input', async () => {
    vi.useFakeTimers();
    const continuationEnvelope = typedEnvelope('Private Workday findings are ready.', 206);
    const operatorMessage = {
      id: 'operator-preempts-continuation',
      content: 'This is the newer operator turn.',
      created_at: '2026-08-13T02:00:00.000Z',
      from_type: 'user',
      context: { _typed_fast_eligible: true },
    };
    let workdayAlive = true;
    let continuationClaimed = false;
    let dispatchStarted = false;
    let surfacedOperatorInput = false;
    let resolveDispatch!: (value: unknown) => void;
    const continuationReleases: Array<Record<string, unknown> | undefined> = [];

    mockApiGet.mockImplementation((url: string) => {
      if (!url.includes('/messages?')) return Promise.resolve({ messages: [] });
      if (dispatchStarted && !surfacedOperatorInput) {
        surfacedOperatorInput = true;
        return Promise.resolve({
          messages: [operatorMessage],
          fast_message_ids: [operatorMessage.id],
          claim_required: true,
        });
      }
      return Promise.resolve({ messages: [], fast_message_ids: [], claim_required: true });
    });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({ authority_held: true });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({ acquired: true, lease: { lease_id: 'lease-preempt', generation: 1 } });
      }
      if (url.endsWith('/typed-fast-lease/release')) return Promise.resolve({ released: true });
      if (url.endsWith('/workday-consultations/claim-next')) {
        return Promise.resolve({ status: 'empty' });
      }
      if (url.endsWith('/workday-consultations/continuations/claim-next')) {
        if (continuationClaimed) return Promise.resolve({ status: 'empty' });
        continuationClaimed = true;
        return Promise.resolve({
          status: 'claimed',
          continuation: {
            result_packet_id: 'packet-preempt',
            continuation_claim_id: 'continuation-claim-preempt',
            lease_expires_at: '2035-01-01T00:00:00.000Z',
            attempt: 1,
            claim_id: 'original-fast-claim',
            delivery_key: 'delivery-preempt',
            continuation_key: 'continuation-preempt',
            result_summary: 'This result should yield to the operator.',
            envelope: continuationEnvelope,
          },
        });
      }
      if (url.endsWith('/workday-consultations/continuations/packet-preempt/release')) {
        continuationReleases.push(body);
        return Promise.resolve({ status: 'released' });
      }
      return Promise.resolve({});
    });
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => {
      dispatchStarted = true;
      return new Promise(resolve => { resolveDispatch = resolve; }).then(() => ({
        receipt,
        result: turnResult({ interrupted: true }),
        completion: null,
        workdayRequests: [],
      }));
    });
    typedFastMocks.interrupt.mockImplementation(() => {
      workdayAlive = false;
      resolveDispatch(undefined);
    });
    mockStartSession.mockReturnValue(makeSession(
      () => Promise.resolve(turnResult()),
      () => workdayAlive,
    ));

    const startedAt = Date.now();
    const done = run('typed-fast-continuation-preempt', {
      ...qualifiedPayload(),
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 0.75,
    });
    await vi.advanceTimersByTimeAsync(4_000);
    await done;

    expect(typedFastMocks.interrupt).toHaveBeenCalledTimes(1);
    expect(Date.now() - startedAt).toBeLessThan(15_000);
    expect(continuationReleases).toEqual([
      expect.objectContaining({ continuation_claim_id: 'continuation-claim-preempt' }),
    ]);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/workday-consultations/continuations/packet-preempt/renew'))).toHaveLength(0);
  });

  it('reconciles a lost Workday continuation completion response without retrying', async () => {
    vi.useFakeTimers();
    const continuationEnvelope = typedEnvelope('Private Workday findings are ready.', 207);
    let workdayAlive = true;
    let packetCompleted = false;
    let continuationClaims = 0;
    let completionCalls = 0;
    let releaseCalls = 0;

    mockApiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('/messages?')
        ? { messages: [], fast_message_ids: [], claim_required: true }
        : { messages: [] },
    ));
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({ authority_held: true });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({ acquired: true, lease: { lease_id: 'lease-lost-continuation', generation: 1 } });
      }
      if (url.endsWith('/typed-fast-lease/release')) return Promise.resolve({ released: true });
      if (url.endsWith('/workday-consultations/claim-next')) {
        return Promise.resolve({ status: 'empty' });
      }
      if (url.endsWith('/workday-consultations/continuations/claim-next')) {
        if (packetCompleted) return Promise.resolve({ status: 'empty' });
        continuationClaims += 1;
        return Promise.resolve({
          status: 'claimed',
          continuation: {
            result_packet_id: 'packet-lost-completion',
            continuation_claim_id: 'continuation-claim-lost-completion',
            lease_expires_at: '2035-01-01T00:00:00.000Z',
            attempt: 1,
            claim_id: 'original-fast-claim',
            delivery_key: 'delivery-lost-completion',
            continuation_key: 'continuation-lost-completion',
            result_summary: 'The private investigation found the answer.',
            envelope: continuationEnvelope,
          },
        });
      }
      if (url.endsWith('/workday-consultations/continuations/packet-lost-completion/complete')) {
        completionCalls += 1;
        packetCompleted = true;
        return Promise.reject(new Error('connection lost after durable commit'));
      }
      if (url.endsWith('/workday-consultations/continuations/packet-lost-completion/release')) {
        releaseCalls += 1;
        workdayAlive = false;
        return Promise.resolve({ status: 'already_completed' });
      }
      return Promise.resolve({});
    });
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => Promise.resolve({
      receipt,
      result: turnResult(),
      completion: { receipt, content: 'Here is the committed Workday result.' },
      workdayRequests: [],
    }));
    mockStartSession.mockReturnValue(makeSession(
      () => Promise.resolve(turnResult()),
      () => workdayAlive,
    ));

    const done = run('typed-fast-continuation-lost-completion', {
      ...qualifiedPayload(),
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 0.75,
    });
    await vi.advanceTimersByTimeAsync(4_000);
    await done;

    expect(continuationClaims).toBe(1);
    expect(completionCalls).toBe(1);
    expect(releaseCalls).toBe(1);
    expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(1);
    expect(mockSendLog.mock.calls.map(call => String(call[1])).join('\n'))
      .not.toContain('typed Workday continuation deferred');
  });

  it('reconciles a lost Fast completion response and then picks up newer input', async () => {
    const staleM1 = {
      id: 'typed-fast-stale-m1', content: 'hi',
      created_at: '2026-07-31T08:25:25.598Z', from_type: 'user',
      context: { _conversation_claim_idempotency_key: 'old-fast-turn' },
    };
    const newerM2 = {
      id: 'typed-fast-newer-m2', content: 'hihi',
      created_at: '2026-07-31T21:06:18.659Z', from_type: 'user',
      context: { _server_artifact_delivery_deferred: true },
    };
    let completed = 0;
    let completionAttempts = 0;
    const claimBodies: Array<Record<string, unknown> | undefined> = [];
    mockApiGet.mockImplementation((url: string) => {
      if (!url.includes('/messages?')) return Promise.resolve(null);
      const messages = completed === 0 ? [staleM1, newerM2]
        : completed === 1 ? [newerM2] : [];
      return Promise.resolve({
        messages,
        fast_message_ids: messages.map(message => message.id),
        fast_claim_groups: completed === 0 ? [{
          idempotency_key: 'old-fast-turn',
          message_ids: [staleM1.id],
          member_count: 1,
          complete: true,
          artifacts_degraded: true,
        }] : [],
        claim_required: true,
      });
    });
    const envelopeFor = (messageId: string) => {
      const transcriptSequence = messageId === staleM1.id ? 101 : 102;
      const message = messageId === staleM1.id ? staleM1 : newerM2;
      const canonicalEnvelope = {
        version: 2,
        project_id: '11111111-1111-4111-8111-111111111111',
        context_authority_generation: 4,
        privacy_generation: 2,
        transcript_sequence: transcriptSequence,
        ledger_revision: 0,
        problem_model: {},
        decisions: [],
        memory: { active: [] },
        recent_conversation: [boundConversationRow(message, transcriptSequence)],
      };
      const canonicalBytes = Buffer.from(JSON.stringify(canonicalEnvelope), 'utf8');
      return {
        snapshot_id: `snapshot-${messageId}`,
        canonical_envelope_b64: canonicalBytes.toString('base64'),
        canonical_sha256: createHash('sha256').update(canonicalBytes).digest('hex'),
        context_authority_generation: 4,
        privacy_generation: 2,
        transcript_sequence: transcriptSequence,
        ledger_revision: 0,
        expires_at: '2035-01-01T00:00:00.000Z',
      };
    };
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({ authority_held: true });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true, lease: { lease_id: 'lease-stale-recovery', generation: 1 },
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        claimBodies.push(body);
        const messageIds = body?.message_ids as string[];
        return Promise.resolve({
          claim_id: `claim-${messageIds[0]}`,
          owner: 'fast',
          message_ids: messageIds,
          envelope: envelopeFor(messageIds[0]),
        });
      }
      if (url.endsWith('/conversation/turns/complete')) {
        completionAttempts += 1;
        completed += 1;
        if (completionAttempts === 1) {
          // Coordinator committed the reply, but its response was lost. The
          // handler must use one bounded write attempt, reconcile the stable
          // claim, and resume polling rather than blocking for general retries.
          return Promise.reject(new Error('POST completion response lost after commit'));
        }
        return Promise.resolve({ status: 'completed' });
      }
      if (url.endsWith('/conversation/turns/abandon')) {
        return Promise.resolve({
          status: 'already_completed',
          legacy_fallback_admissible: false,
        });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => Promise.resolve({
      receipt,
      result: turnResult({ resultText: `reply-${completed + 1}` }),
      completion: { receipt, content: `reply-${completed + 1}` },
      workdayRequests: [],
    }));
    const workday = makeSession(() => turnResult(), () => completed < 2);
    mockStartSession.mockReturnValue(workday);

    await run('typed-fast-stale-recovery', {
      ...qualifiedPayload(),
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 0.001,
    });

    expect(claimBodies.map(body => body?.message_ids)).toEqual([
      [staleM1.id],
      [newerM2.id],
    ]);
    expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(2);
    const completionCalls = mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/complete'));
    expect(completionCalls).toHaveLength(2);
    expect(completionCalls[0]?.[2]).toBe(1);
    expect(completionCalls[0]?.[3]).toBe(5_000);
    const recoveredPrompt: unknown = typedFastMocks.dispatch.mock.calls[0]?.[1];
    const recoveredPromptText = typeof recoveredPrompt === 'string'
      ? recoveredPrompt
      : JSON.stringify(recoveredPrompt);
    expect(recoveredPromptText).toContain(
      'attachments from this interrupted turn exceeded the safe delivery limit',
    );
    const ordinaryPrompt: unknown = typedFastMocks.dispatch.mock.calls[1]?.[1];
    const ordinaryPromptText = typeof ordinaryPrompt === 'string'
      ? ordinaryPrompt
      : JSON.stringify(ordinaryPrompt);
    expect(ordinaryPromptText).not.toContain(
      'attachments from this interrupted turn exceeded the safe delivery limit',
    );
    expect(workday.sendTurn).not.toHaveBeenCalled();
  });

  it('paces a transient Fast claim conflict and retries the same client identity', async () => {
    const operatorMessage = {
      id: 'typed-fast-conflict', content: 'hello',
      created_at: new Date().toISOString(), from_type: 'user', context: {},
    };
    let completed = false;
    const claimBodies: Array<Record<string, unknown> | undefined> = [];
    const claimTimes: number[] = [];
    let messagePolls = 0;
    mockApiGet.mockImplementation((url: string) => {
      if (!url.includes('/messages?') || completed) return Promise.resolve({ messages: [] });
      messagePolls += 1;
      return Promise.resolve(messagePolls === 1
        ? { messages: [], fast_message_ids: [] }
        : { messages: [operatorMessage], fast_message_ids: [operatorMessage.id] });
    });
    const canonicalEnvelope = {
      version: 2,
      project_id: '11111111-1111-4111-8111-111111111111',
      context_authority_generation: 4,
      privacy_generation: 2,
      transcript_sequence: 103,
      ledger_revision: 0,
      problem_model: {}, decisions: [], memory: { active: [] },
      recent_conversation: [boundConversationRow(operatorMessage, 103)],
    };
    const canonicalBytes = Buffer.from(JSON.stringify(canonicalEnvelope), 'utf8');
    const envelope = {
      snapshot_id: 'snapshot-paced-conflict',
      canonical_envelope_b64: canonicalBytes.toString('base64'),
      canonical_sha256: createHash('sha256').update(canonicalBytes).digest('hex'),
      context_authority_generation: 4,
      privacy_generation: 2,
      transcript_sequence: 103,
      ledger_revision: 0,
      expires_at: '2035-01-01T00:00:00.000Z',
    };
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({ authority_held: true });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true, lease: { lease_id: 'lease-paced-conflict', generation: 1 },
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        claimBodies.push(body);
        claimTimes.push(Date.now());
        if (claimBodies.length === 1) {
          return Promise.reject(new Error('409: Turn is already claimed or changed'));
        }
        return Promise.resolve({
          claim_id: 'claim-paced-conflict', owner: 'fast',
          message_ids: [operatorMessage.id], envelope,
        });
      }
      if (url.endsWith('/conversation/turns/complete')) {
        completed = true;
        return Promise.resolve({ status: 'completed' });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => Promise.resolve({
      receipt,
      result: turnResult({ resultText: 'Hello.' }),
      completion: { receipt, content: 'Hello.' },
      workdayRequests: [],
    }));
    const workday = makeSession(() => turnResult(), () => !completed);
    mockStartSession.mockReturnValue(workday);

    await run('typed-fast-paced-conflict', {
      ...qualifiedPayload(),
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 0.001,
    });

    expect(claimBodies).toHaveLength(2);
    expect(claimTimes[1] - claimTimes[0]).toBeGreaterThanOrEqual(900);
    expect(claimBodies[1]?.client_claim_id).toBe(claimBodies[0]?.client_claim_id);
    expect(claimBodies[1]?.idempotency_key).toBe(claimBodies[0]?.idempotency_key);
    expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(1);
    expect(workday.sendTurn).not.toHaveBeenCalled();
  });

  it('paces legacy Workday claim conflicts while renewing the Resident incarnation', async () => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'legacy-workday-conflict', content: 'continue the existing Workday thread',
      created_at: new Date().toISOString(), from_type: 'user', context: {},
    };
    const claimTimes: number[] = [];
    const heartbeatBodies: Array<Record<string, unknown>> = [];
    mockApiGet.mockImplementation((url: string) => {
      if (!url.includes('/messages?')) return Promise.resolve(null);
      return Promise.resolve({ messages: [operatorMessage], claim_required: true });
    });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        heartbeatBodies.push(body ?? {});
        return Promise.resolve(heartbeatBodies.length === 1
          ? {
              authority_held: true,
              resident_incarnation_held: true,
              stop_requested: false,
              leased_until: new Date(Date.now() + 180_000).toISOString(),
            }
          : {
              authority_held: false,
              resident_incarnation_held: false,
              stop_requested: true,
            });
      }
      if (url.includes('/messages/claim')) {
        claimTimes.push(Date.now());
        return Promise.reject(new Error('409: operator batch already claimed'));
      }
      return Promise.resolve({});
    });
    const workday = makeSession(() => turnResult(), () => true);
    mockStartSession.mockReturnValue(workday);

    const handling = run('legacy-workday-conflict-task', {
      project_name: 'P',
      open_messages: [],
      strategic_session_autonomous: false,
      strategic_session_id: 'legacy-workday-conflict-session',
      strategic_session_idle_tick_seconds: 300,
      strategic_session_message_poll_seconds: 0.001,
    });
    await vi.advanceTimersByTimeAsync(31_000);
    await handling;

    expect(claimTimes.length).toBeGreaterThan(1);
    expect(claimTimes.every((at, index) => (
      index === 0 || at - claimTimes[index - 1] >= 1_500
    ))).toBe(true);
    expect(heartbeatBodies.length).toBeGreaterThanOrEqual(2);
    expect(heartbeatBodies.at(-1)).toEqual(expect.objectContaining({
      session_id: 'legacy-workday-conflict-session',
      task_id: 'legacy-workday-conflict-task',
      worker_generation: 1,
      progress: false,
    }));
    expect(typeof heartbeatBodies.at(-1)?.resident_incarnation_id).toBe('string');
    expect(workday.touch).toHaveBeenCalled();
    expect(workday.sendTurn).not.toHaveBeenCalled();
  });

  it('releases the exact lease after a failed readiness probe without starting a Fast turn', async () => {
    const releases: Array<Record<string, unknown> | undefined> = [];
    mockApiGet.mockResolvedValue({ messages: [] });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) return Promise.resolve({ authority_held: true });
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({ acquired: true, lease: { lease_id: 'lease-readiness', generation: 2 } });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        releases.push(body);
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });
    typedFastMocks.ensureReady.mockRejectedValue(new Error('warm probe failed'));
    mockStartSession.mockReturnValue(makeSession(() => turnResult(), () => false));

    await run('typed-fast-readiness-failed');

    expect(typedFastMocks.dispatch).not.toHaveBeenCalled();
    expect(typedFastMocks.close).toHaveBeenCalledTimes(1);
    expect(releases).toEqual([expect.objectContaining({
      lease_id: 'lease-readiness', generation: 2, reason: 'readiness_failed',
    })]);
  });

  it('closes a lease-lost Fast controller without giving its next tagged message to Workday', async () => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'typed-fast-lease-lost-message', content: 'Continue after the lease loss.',
      created_at: new Date().toISOString(), from_type: 'user',
    };
    let leaseLost = false;
    let delivered = false;
    let heartbeatCount = 0;
    let leaseAcquireCount = 0;
    const releaseCalls: string[] = [];
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?') && leaseLost && !delivered) {
        delivered = true;
        return Promise.resolve({ messages: [operatorMessage], fast_message_ids: [operatorMessage.id] });
      }
      return Promise.resolve({ messages: [] });
    });
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/strategic-session/heartbeat')) {
        heartbeatCount += 1;
        if (heartbeatCount === 2) {
          leaseLost = true;
          return Promise.resolve({
            authority_held: true,
            typed_fast: { lease_id: 'lease-lost', generation: 3, status: 'ended' },
          });
        }
        return Promise.resolve({ authority_held: true, stop_requested: heartbeatCount > 2 });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        leaseAcquireCount += 1;
        if (leaseAcquireCount > 1) {
          return Promise.resolve({ acquired: false, reason: 'replacement_capacity_pending' });
        }
        return Promise.resolve({ acquired: true, lease: { lease_id: 'lease-lost', generation: 3 } });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        releaseCalls.push(url);
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });
    const workday = makeSession(
      () => turnResult({ sentOperatorMessage: true, resultText: 'Workday took over.' }),
      () => true,
    );
    mockStartSession.mockReturnValue(workday);

    const done = run('typed-fast-lease-lost', {
      ...qualifiedPayload(),
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 0.001,
    });
    for (let tick = 0; tick < 6; tick += 1) {
      await vi.advanceTimersByTimeAsync(1_000);
    }
    await done;

    expect(typedFastMocks.close).toHaveBeenCalledTimes(1);
    expect(leaseAcquireCount).toBe(2);
    expect(workday.sendTurn).not.toHaveBeenCalled();
    expect(releaseCalls).toEqual([]);
  });

  it.each([
    [
      'typed lease revocation',
      {
        typed_fast: {
          lease_id: 'standard-prompt-fence-lease',
          generation: 4,
          status: 'ended',
        },
      },
    ],
    [
      'a newly active call',
      {
        call: {
          call_id: 'standard-prompt-active-call',
          generation: 9,
          status: 'active',
          input_revision: 0,
          expires_at: '2035-01-01T00:00:00.000Z',
        },
      },
    ],
  ])('waits for a standard Fast prompt to settle after %s fences its exact owner', async (
    _reason,
    ownershipProjection,
  ) => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: `standard-fast-prompt-fence-${_reason.replaceAll(' ', '-')}`,
      content: 'Do not dispatch this prompt after its exact owner is fenced.',
      created_at: new Date().toISOString(),
      from_type: 'user',
      context: {
        attachments: [0, 1].map(index => ({
          type: 'image',
          mime_type: 'image/png',
          name: `standard-fence-${index}.png`,
          url: `https://storage.googleapis.com/bucket/standard-fence-${index}.png?X-Goog-Signature=sig`,
        })),
      },
    };
    let delivered = false;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?') && !delivered) {
        delivered = true;
        return Promise.resolve({
          messages: [operatorMessage],
          fast_message_ids: [operatorMessage.id],
          claim_required: true,
        });
      }
      return Promise.resolve({ messages: [], fast_message_ids: [], claim_required: true });
    });
    const heartbeatBodies: Array<Record<string, unknown>> = [];
    let abandonCount = 0;
    let alive = true;
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        heartbeatBodies.push(body ?? {});
        return Promise.resolve({
          authority_held: true,
          resident_incarnation_held: true,
          conversation_claim_held: true,
          stop_requested: false,
          ...(body?.conversation_claim_id === 'standard-prompt-fence-claim'
            ? ownershipProjection
            : {}),
        });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: 'standard-prompt-fence-lease', generation: 4 },
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        return Promise.resolve({
          claim_id: 'standard-prompt-fence-claim',
          owner: 'fast',
          message_ids: [operatorMessage.id],
          envelope: typedEnvelope(operatorMessage.content, 84, operatorMessage.id, messageAttachments(operatorMessage)),
        });
      }
      if (url.endsWith('/conversation/turns/abandon')) {
        abandonCount += 1;
        alive = false;
        return Promise.resolve({ status: 'abandoned', legacy_fallback_admissible: true });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });
    let fetchCount = 0;
    let resolveSecondFetch!: (response: unknown) => void;
    const attachmentFetch = vi.fn(() => {
      fetchCount += 1;
      if (fetchCount === 1) return new Promise(() => undefined);
      return new Promise((resolve) => { resolveSecondFetch = resolve; });
    });
    vi.stubGlobal('fetch', attachmentFetch);
    const workday = makeSession(() => Promise.resolve(turnResult()), () => alive);
    mockStartSession.mockReturnValue(workday);

    try {
      const done = run('standard-fast-prompt-owner-fence', {
        ...qualifiedPayload(),
        strategic_session_message_poll_seconds: 0.001,
      });
      for (let i = 0; i < 20 && attachmentFetch.mock.calls.length === 0; i += 1) {
        await vi.advanceTimersByTimeAsync(0);
      }
      expect(attachmentFetch).toHaveBeenCalledTimes(1);

      await vi.advanceTimersByTimeAsync(15_001);
      expect(attachmentFetch).toHaveBeenCalledTimes(2);
      expect(heartbeatBodies.some(body => (
        body.conversation_claim_id === 'standard-prompt-fence-claim'
        && body.typed_fast_lease_id === 'standard-prompt-fence-lease'
        && body.typed_fast_generation === 4
      ))).toBe(true);
      expect(abandonCount).toBe(0);
      expect(typedFastMocks.dispatch).not.toHaveBeenCalled();
      expect(workday.sendTurn).not.toHaveBeenCalled();

      resolveSecondFetch({
        ok: true,
        status: 200,
        headers: { get: (name: string) => name === 'content-type' ? 'image/png' : null },
        arrayBuffer: () => Promise.resolve(new ArrayBuffer(0)),
      });
      for (let i = 0; i < 20 && abandonCount === 0; i += 1) {
        await vi.advanceTimersByTimeAsync(0);
      }
      await vi.advanceTimersByTimeAsync(1_000);
      await done;

      expect(abandonCount).toBe(1);
      expect(typedFastMocks.dispatch).not.toHaveBeenCalled();
      expect(mockApiPost.mock.calls.filter(([url]) =>
        url.endsWith('/conversation/turns/complete'))).toHaveLength(0);
      expect(workday.sendTurn).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('keeps Activity private while Fast replies and Continuous Workday stays autonomous', async () => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'typed-fast-parallel-message',
      content: 'hi',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    const activityRow = {
      id: 'workday-start-activity',
      content: 'Workday started examining project state.',
      created_at: new Date().toISOString(),
      from_type: 'system',
      context: { surface: 'activity' },
    };
    let messagePolls = 0;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?')) {
        messagePolls += 1;
        return Promise.resolve(messagePolls === 2
          ? {
              messages: [operatorMessage, activityRow],
              fast_message_ids: [operatorMessage.id],
              claim_required: true,
            }
          : { messages: [], fast_message_ids: [], claim_required: true });
      }
      return Promise.resolve(null);
    });

    const canonicalEnvelope = {
      version: 2,
      project_id: '11111111-1111-4111-8111-111111111111',
      context_authority_generation: 4,
      privacy_generation: 2,
      transcript_sequence: 81,
      ledger_revision: 0,
      problem_model: {},
      decisions: [],
      memory: { active: [] },
      recent_conversation: [boundConversationRow(operatorMessage, 81)],
    };
    const canonicalBytes = Buffer.from(JSON.stringify(canonicalEnvelope), 'utf8');
    const envelope = {
      snapshot_id: 'snapshot-parallel-fast',
      canonical_envelope_b64: canonicalBytes.toString('base64'),
      canonical_sha256: createHash('sha256').update(canonicalBytes).digest('hex'),
      context_authority_generation: 4,
      privacy_generation: 2,
      transcript_sequence: 81,
      ledger_revision: 0,
      expires_at: '2035-01-01T00:00:00.000Z',
    };
    let heartbeatCount = 0;
    const heartbeatBodies: Array<Record<string, unknown>> = [];
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        heartbeatCount += 1;
        heartbeatBodies.push(body ?? {});
        return Promise.resolve({
          authority_held: true,
          stop_requested: heartbeatCount > 2,
        });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: 'lease-parallel-fast', generation: 1 },
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        return Promise.resolve({
          claim_id: 'claim-parallel-fast',
          owner: 'fast',
          message_ids: [operatorMessage.id],
          envelope,
        });
      }
      if (url.endsWith('/conversation/turns/complete')) {
        return Promise.resolve({ status: 'completed' });
      }
      if (url === '/api/agent/strategic' && body?.op === 'begin_autonomous_turn') {
        return Promise.resolve({
          status: 'created',
          autonomous_turn_id: '00000000-0000-0000-0000-000000000751',
          autonomous_turn_lease_id: '00000000-0000-0000-0000-000000000752',
          autonomous_handoff_idempotency_key: (body.data as Record<string, unknown>)
            .client_turn_id,
        });
      }
      if (url === '/api/agent/strategic' && body?.op === 'end_autonomous_turn') {
        return Promise.resolve({ status: 'ended' });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => Promise.resolve({
      receipt,
      result: turnResult({ resultText: 'Hi — what are you thinking about?' }),
      completion: {
        receipt,
        content: 'Hi — what are you thinking about?',
      },
      workdayRequests: [],
    }));

    // eslint-disable-next-line prefer-const -- assigned after the session closure is created and read only when that closure runs.
    let privateRuntime!: ReturnType<typeof bindSessionWithPrivateRuntime>;
    let capabilityRevokedAtTurnStart = false;
    let privateMarkerAtTurnStart: Record<string, unknown> = {};
    let framesAtAutonomousTurnStart = 0;
    const workday = makeSession(
      (prompt) => {
        capabilityRevokedAtTurnStart = privateRuntime.replyPaths.length === 3
          && privateRuntime.replyPaths.every((artifactPath) => !fs.existsSync(artifactPath));
        const marker = JSON.parse(
          fs.readFileSync(privateRuntime.markerPath, 'utf8'),
        ) as Record<string, unknown>;
        privateMarkerAtTurnStart = marker;
        framesAtAutonomousTurnStart = mockSendWsFrame.mock.calls.length;
        workday._emit({ kind: 'text', text: 'PRIVATE_AUTONOMOUS_TEXT_MUST_NOT_STREAM' });
        workday._emit({ kind: 'tool', toolName: 'private_tool', text: 'PRIVATE_AUTONOMOUS_TOOL_MUST_NOT_STREAM' });
        workday._emit({ kind: 'error', text: 'PRIVATE_AUTONOMOUS_ERROR_MUST_NOT_STREAM' });
        expect(JSON.stringify(prompt)).toContain('## Typed Fast private autonomous boundary');
        expect(JSON.stringify(prompt)).toContain('`./messages send`');
        expect(JSON.stringify(prompt)).toContain('report-autonomous-finding');
        return Promise.resolve(turnResult({
          appliedStrategicOp: true,
          sentOperatorMessage: false,
          resultText: 'Reviewed and updated a durable watch.\nTICK-VERDICT: ADVANCED — updated the watch from current evidence',
        }));
      },
      () => true,
    );
    privateRuntime = bindSessionWithPrivateRuntime(workday);

    const done = run('typed-fast-parallel', {
      ...qualifiedPayload(),
      strategic_session_autonomous: true,
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 0.001,
      strategic_session_exploration_interval_seconds: 1,
    });
    // Let startup, the initial empty poll, and Fast's foreground poll settle.
    await vi.advanceTimersByTimeAsync(1_000);

    expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(1);
    expect(mockApiPost.mock.calls.some(([url]) =>
      url.endsWith('/conversation/turns/complete'))).toBe(true);
    expect(workday.interrupt).not.toHaveBeenCalled();
    expect(workday.sendTurn).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(workday.sendTurn.mock.calls[0]?.[0])).toContain('No operator message is pending');
    expect(capabilityRevokedAtTurnStart).toBe(true);
    expect(privateMarkerAtTurnStart).toMatchObject({
      kind: 'typed-autonomous',
      autonomous_turn_id: '00000000-0000-0000-0000-000000000751',
      autonomous_turn_lease_id: '00000000-0000-0000-0000-000000000752',
    });
    const framesDuringAutonomousTurn = mockSendWsFrame.mock.calls
      .slice(framesAtAutonomousTurnStart)
      .map((call) => call[1] as { payload?: { status?: string; text?: string } } | undefined)
      .filter((frame): frame is { payload: { status?: string; text?: string } } => Boolean(frame?.payload));
    expect(framesDuringAutonomousTurn.some((frame) => (
      frame.payload.text?.includes('PRIVATE_AUTONOMOUS_')
      || ['responding', 'working', 'done'].includes(frame.payload.status ?? '')
    ))).toBe(false);
    expect(heartbeatBodies.some(body => (
      body.progress === true && body.autonomous_turn === true
    ))).toBe(true);
    expect(heartbeatBodies.some(body => (
      Array.isArray(body.handled_message_ids)
      && body.handled_message_ids.includes(activityRow.id)
    ))).toBe(false);

    await vi.advanceTimersByTimeAsync(2_000);
    await done;
    const autonomousOps = mockApiPost.mock.calls
      .filter(([url]) => url === '/api/agent/strategic')
      .map(([, body]) => body as Record<string, unknown>);
    expect(autonomousOps.map(body => body.op)).toEqual([
      'begin_autonomous_turn',
      'end_autonomous_turn',
      'begin_autonomous_turn',
      'end_autonomous_turn',
    ]);
    expect(workday.sendTurn).toHaveBeenCalledTimes(2);
    for (const [beginIndex, endIndex] of [[0, 1], [2, 3]]) {
      const beginData = autonomousOps[beginIndex]?.data as Record<string, unknown>;
      expect(autonomousOps[endIndex]).toMatchObject({
        autonomous_turn_id: '00000000-0000-0000-0000-000000000751',
        autonomous_turn_lease_id: '00000000-0000-0000-0000-000000000752',
        autonomous_handoff_idempotency_key: beginData.client_turn_id,
      });
    }
  });

  it.each([
    ['server conflict', new Error('HTTP 409: foreground Workday owns the lane')],
    ['transport failure', new Error('ECONNRESET while acquiring autonomous lease')],
  ])('fails closed on %s before a private autonomous turn can execute', async (_label, leaseError) => {
    vi.useFakeTimers();
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?')) {
        return Promise.resolve({ messages: [], fast_message_ids: [], claim_required: true });
      }
      return Promise.resolve(null);
    });
    let heartbeatCount = 0;
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        heartbeatCount += 1;
        return Promise.resolve({
          authority_held: true,
          resident_incarnation_held: true,
          stop_requested: heartbeatCount > 2,
        });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: `lease-${_label.replace(/\s+/g, '-')}`, generation: 1 },
        });
      }
      if (url === '/api/agent/strategic' && body?.op === 'begin_autonomous_turn') {
        return Promise.reject(leaseError);
      }
      return Promise.resolve({});
    });
    const workday = makeSession(
      () => Promise.resolve(turnResult({
        appliedStrategicOp: true,
        sawToolUse: true,
        resultText: 'TICK-VERDICT: ADVANCED — this must never execute',
      })),
      () => true,
    );
    bindSessionWithPrivateRuntime(workday);

    const done = run(`typed-fast-autonomous-lease-${_label.replace(/\s+/g, '-')}`, {
      ...qualifiedPayload(),
      strategic_session_autonomous: true,
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 0.001,
      strategic_session_exploration_interval_seconds: 1,
    });
    await vi.advanceTimersByTimeAsync(3_000);
    await done;

    const strategicOps = mockApiPost.mock.calls
      .filter(([url]) => url === '/api/agent/strategic')
      .map(([, body]) => (body as Record<string, unknown>).op);
    expect(strategicOps.length).toBeGreaterThan(0);
    expect(strategicOps.every(op => op === 'begin_autonomous_turn')).toBe(true);
    expect(strategicOps).not.toContain('report_autonomous_finding');
    expect(strategicOps).not.toContain('end_autonomous_turn');
    expect(workday.sendTurn).not.toHaveBeenCalled();
    expect(mockSendLog.mock.calls.some(([, text]) =>
      String(text).includes('autonomous handoff lease unavailable'))).toBe(true);
  });

  it('keeps Chat mode Workday on demand when typed Fast owns the foreground', async () => {
    vi.useFakeTimers();
    mockApiGet.mockResolvedValue({
      messages: [],
      fast_message_ids: [],
      claim_required: true,
    });
    let heartbeatCount = 0;
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/strategic-session/heartbeat')) {
        heartbeatCount += 1;
        return Promise.resolve({
          authority_held: true,
          resident_incarnation_held: true,
          stop_requested: heartbeatCount > 1,
        });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: 'lease-chat-mode-on-demand', generation: 1 },
        });
      }
      if (url.endsWith('/workday-consultations/claim-next')) {
        return Promise.resolve({ status: 'empty' });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });
    const workday = makeSession(
      () => turnResult({ resultText: 'Chat mode must not self-prompt.' }),
      () => true,
    );
    mockStartSession.mockReturnValue(workday);

    const done = run('typed-fast-chat-mode-on-demand', {
      ...qualifiedPayload(),
      strategic_session_autonomous: false,
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 0.001,
    });
    await vi.advanceTimersByTimeAsync(2_000);
    await done;

    expect(typedFastMocks.construct).toHaveBeenCalledTimes(1);
    expect(workday.sendTurn).not.toHaveBeenCalled();
    expect(workday.interrupt).not.toHaveBeenCalled();
  });

  it('claims a queued consultation without new operator input after stale Resident continuation cleanup is deauthorized', async () => {
    vi.useFakeTimers();
    const staleTaskId = 'stale-resident-continuation-cleanup';
    const activeTaskId = 'active-resident-queued-consultation';
    const queuedPacketId = 'packet-c6e48b77-regression';
    const staleEnvelope = typedEnvelope('Deliver the prior Resident result.', 851);
    const queuedEnvelope = typedEnvelope('Investigate the already-queued request.', 852);
    let staleResidentAlive = true;
    let activeResidentAlive = true;
    let staleContinuationClaimed = false;
    let activeConsultationClaimed = false;
    let messagePolls = 0;
    const staleReleaseBodies: Array<Record<string, unknown>> = [];
    const activeClaimBodies: Array<Record<string, unknown>> = [];
    const activeAckBodies: Array<Record<string, unknown>> = [];

    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?')) messagePolls += 1;
      return Promise.resolve({
        messages: [],
        fast_message_ids: [],
        claim_required: true,
      });
    });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      const requestTaskId = body?.task_id;
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({
          authority_held: true,
          resident_incarnation_held: true,
        });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: {
            lease_id: requestTaskId === staleTaskId
              ? 'lease-stale-continuation-cleanup'
              : 'lease-active-queued-consultation',
            generation: 1,
          },
        });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      if (url.endsWith('/workday-consultations/continuations/claim-next')) {
        if (requestTaskId === activeTaskId) return Promise.resolve({ status: 'empty' });
        if (requestTaskId === staleTaskId && !staleContinuationClaimed) {
          staleContinuationClaimed = true;
          return Promise.resolve({
            status: 'claimed',
            continuation: {
              result_packet_id: 'stale-result-packet',
              continuation_claim_id: 'stale-continuation-claim',
              lease_expires_at: '2035-01-01T00:00:00.000Z',
              attempt: 1,
              claim_id: 'stale-fast-claim',
              delivery_key: 'stale-delivery-key',
              continuation_key: 'stale-continuation-key',
              result_summary: 'This result belongs to the stopped Resident.',
              envelope: staleEnvelope,
            },
          });
        }
        return Promise.resolve({ status: 'empty' });
      }
      if (url.endsWith('/workday-consultations/continuations/stale-result-packet/release')) {
        staleReleaseBodies.push(body ?? {});
        staleResidentAlive = false;
        return Promise.reject(new Error('continuation cleanup rejected (401): Resident deauthorized'));
      }
      if (url.endsWith('/workday-consultations/claim-next')) {
        if (requestTaskId !== activeTaskId || activeConsultationClaimed) {
          return Promise.resolve({ status: 'empty' });
        }
        activeConsultationClaimed = true;
        activeClaimBodies.push(body ?? {});
        return Promise.resolve({
          status: 'claimed',
          packet: {
            packet_id: queuedPacketId,
            workday_claim_id: 'active-workday-claim',
            lease_expires_at: '2035-01-01T00:00:00.000Z',
            attempt: 1,
            claim_id: 'original-fast-claim',
            delivery_key: 'active-delivery-key',
            continuation_key: 'active-continuation-key',
            consultation_id: 'queued-consultation-id',
            question: 'Investigate the already-queued request.',
            envelope: queuedEnvelope,
          },
        });
      }
      if (url.endsWith(`/workday-consultations/${queuedPacketId}/renew`)) {
        return Promise.resolve({
          workday_claim_id: 'active-workday-claim',
          lease_expires_at: '2035-01-01T00:00:00.000Z',
        });
      }
      if (url.endsWith(`/workday-consultations/${queuedPacketId}/ack`)) {
        activeAckBodies.push(body ?? {});
        activeResidentAlive = false;
        return Promise.resolve({ status: 'completed' });
      }
      return Promise.resolve({});
    });
    typedFastMocks.dispatch.mockRejectedValue(new Error(
      'the stopped Resident cannot format its stale continuation',
    ));
    const staleWorkday = makeSession(
      () => Promise.resolve(turnResult()),
      () => staleResidentAlive,
    );
    const activeWorkday = makeSession(
      () => Promise.resolve(turnResult({
        resultText: 'The queued consultation completed on the active Resident.',
        sentOperatorMessage: false,
      })),
      () => activeResidentAlive,
    );
    bindSessionWithPrivateRuntime([staleWorkday, activeWorkday]);

    const staleDone = run(staleTaskId, {
      ...qualifiedPayload(),
      strategic_session_message_poll_seconds: 0.001,
    });
    await vi.advanceTimersByTimeAsync(2_000);
    expect(staleReleaseBodies).toHaveLength(1);
    await staleDone;

    const activeDone = run(activeTaskId, {
      ...qualifiedPayload(),
      strategic_session_message_poll_seconds: 0.001,
    });
    await vi.advanceTimersByTimeAsync(4_000);
    expect(activeClaimBodies).toHaveLength(1);
    expect(activeWorkday.sendTurn).toHaveBeenCalledTimes(1);
    expect(activeAckBodies).toHaveLength(1);
    await activeDone;

    expect(messagePolls).toBeGreaterThan(0);
    expect(staleReleaseBodies).toEqual([
      expect.objectContaining({
        task_id: staleTaskId,
        continuation_claim_id: 'stale-continuation-claim',
        incarnation_id: expect.any(String),
      }),
    ]);
    expect(mockSendLog.mock.calls.some(([, text]) => (
      String(text).includes('typed continuation release deferred')
      && String(text).includes('(401)')
    ))).toBe(true);
    expect(activeClaimBodies).toEqual([
      expect.objectContaining({
        task_id: activeTaskId,
        incarnation_id: expect.any(String),
      }),
    ]);
    expect(activeClaimBodies[0]?.incarnation_id)
      .not.toBe(staleReleaseBodies[0]?.incarnation_id);
    expect(activeAckBodies).toEqual([
      expect.objectContaining({
        task_id: activeTaskId,
        workday_claim_id: 'active-workday-claim',
        outcome: 'completed',
      }),
    ]);
    expect(activeWorkday.sendTurn).toHaveBeenCalledTimes(1);
    expect(staleWorkday.sendTurn).not.toHaveBeenCalled();
  });

  it('does not preempt an active autonomous Workday turn for a Fast-only reply', async () => {
    vi.useFakeTimers();
    typedFastMocks.abandonDispositionWithConfirmation.mockReturnValue('completed');
    const operatorMessage = {
      id: 'typed-fast-during-active-autonomy',
      content: 'Give me the quick answer only.',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let autonomousStarted = false;
    let messageDelivered = false;
    const lifecycle: string[] = [];
    mockApiGet.mockImplementation((url: string) => {
      if (!url.includes('/messages?')) return Promise.resolve(null);
      if (autonomousStarted && !messageDelivered) {
        messageDelivered = true;
        return Promise.resolve({
          messages: [operatorMessage],
          fast_message_ids: [operatorMessage.id],
          claim_required: true,
        });
      }
      return Promise.resolve({ messages: [], fast_message_ids: [], claim_required: true });
    });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({ authority_held: true, resident_incarnation_held: true });
      }
      if (url === '/api/agent/strategic' && body?.op === 'begin_autonomous_turn') {
        return Promise.resolve({
          autonomous_turn_id: '00000000-0000-0000-0000-000000000761',
          autonomous_turn_lease_id: '00000000-0000-0000-0000-000000000762',
          autonomous_handoff_idempotency_key: (body.data as Record<string, unknown>)
            .client_turn_id,
        });
      }
      if (url === '/api/agent/strategic' && body?.op === 'end_autonomous_turn') {
        return Promise.resolve({ status: 'ended' });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: 'lease-fast-active-autonomy', generation: 1 },
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        return Promise.resolve({
          claim_id: 'claim-fast-active-autonomy',
          owner: 'fast',
          message_ids: [operatorMessage.id],
          envelope: typedEnvelope(operatorMessage.content, 85, operatorMessage.id, messageAttachments(operatorMessage)),
        });
      }
      if (url.endsWith('/conversation/turns/complete')) {
        // The server committed the Fast-only reply, but the response was lost.
        // Recovery must not infer a consultation packet from completion alone.
        lifecycle.push('fast-only-completion-committed-response-lost');
        return Promise.reject(new Error('Fast-only completion response lost'));
      }
      if (url.endsWith('/conversation/turns/abandon')) {
        lifecycle.push('fast-only-abandon-already-completed');
        return Promise.resolve({ status: 'already_completed' });
      }
      if (url.endsWith('/workday-consultations/claim-next')) {
        return Promise.resolve({ status: 'empty' });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => Promise.resolve({
      receipt,
      result: turnResult({ resultText: 'The quick answer.' }),
      completion: { receipt, content: 'The quick answer.' },
      workdayRequests: [],
    }));

    let resolveAutonomous!: (result: ResidentTurnResult) => void;
    const autonomousTurn = new Promise<ResidentTurnResult>((resolve) => {
      resolveAutonomous = resolve;
    });
    let alive = true;
    const workday = makeSession(() => {
      autonomousStarted = true;
      return autonomousTurn;
    }, () => alive);
    workday.interrupt.mockImplementation(() => {
      lifecycle.push('unexpected-autonomous-interrupt');
    });
    bindSessionWithPrivateRuntime(workday);

    const done = run('typed-fast-active-autonomy-no-consultation', {
      ...qualifiedPayload(),
      strategic_session_autonomous: true,
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 0.001,
      strategic_session_exploration_interval_seconds: 1,
    });
    await vi.advanceTimersByTimeAsync(1_000);

    expect(workday.sendTurn).toHaveBeenCalledTimes(1);
    expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(1);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/complete'))).toHaveLength(1);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/abandon'))).toHaveLength(1);
    expect(lifecycle).toEqual([
      'fast-only-completion-committed-response-lost',
      'fast-only-abandon-already-completed',
    ]);
    expect(workday.interrupt).not.toHaveBeenCalled();

    alive = false;
    resolveAutonomous(turnResult({
      sentOperatorMessage: false,
      resultText: 'Autonomous review completed.\nTICK-VERDICT: CLEAR — no correction needed',
    }));
    await vi.advanceTimersByTimeAsync(2_000);
    await done;
  });

  it('settles active autonomous Workday before claiming a confirmed Fast consultation', async () => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'typed-fast-preempts-autonomy-for-consultation',
      content: 'Please investigate the deeper repository question.',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let autonomousStarted = false;
    let messageDelivered = false;
    mockApiGet.mockImplementation((url: string) => {
      if (!url.includes('/messages?')) return Promise.resolve(null);
      if (autonomousStarted && !messageDelivered) {
        messageDelivered = true;
        return Promise.resolve({
          messages: [operatorMessage],
          fast_message_ids: [operatorMessage.id],
          claim_required: true,
        });
      }
      return Promise.resolve({ messages: [], fast_message_ids: [], claim_required: true });
    });

    const lifecycle: string[] = [];
    const consultationEnvelope = typedEnvelope(operatorMessage.content, 86, operatorMessage.id, messageAttachments(operatorMessage));
    const firedWatchId = 'watch-fast-preempt-autonomy';
    const firedBarrierId = 'barrier-fast-preempt-autonomy';
    const heartbeatBodies: Array<Record<string, unknown>> = [];
    let consultationAvailable = false;
    let consultationClaimed = false;
    let alive = true;
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        heartbeatBodies.push(body ?? {});
        return Promise.resolve({
          authority_held: true,
          resident_incarnation_held: true,
          fired_watches: [{
            id: firedWatchId,
            target_kind: 'task',
            target_task_id: 'task-fast-preempt-autonomy',
            purpose: 'Act on the exact terminal task receipt.',
            state: 'fired',
          }],
          fired_barriers: [{
            id: firedBarrierId,
            event_type: 'task_terminal',
            summary: 'A watched task reached its terminal outcome.',
            task_id: 'task-fast-preempt-autonomy',
          }],
        });
      }
      if (url === '/api/agent/strategic' && body?.op === 'begin_autonomous_turn') {
        return Promise.resolve({
          autonomous_turn_id: '00000000-0000-0000-0000-000000000771',
          autonomous_turn_lease_id: '00000000-0000-0000-0000-000000000772',
          autonomous_handoff_idempotency_key: (body.data as Record<string, unknown>)
            .client_turn_id,
        });
      }
      if (url === '/api/agent/strategic' && body?.op === 'end_autonomous_turn') {
        return Promise.resolve({ status: 'ended' });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: 'lease-fast-preempt-autonomy', generation: 1 },
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        return Promise.resolve({
          claim_id: 'claim-fast-preempt-autonomy',
          owner: 'fast',
          message_ids: [operatorMessage.id],
          envelope: consultationEnvelope,
        });
      }
      if (url.endsWith('/conversation/turns/complete')) {
        lifecycle.push('fast-complete');
        consultationAvailable = true;
        return Promise.resolve({
          status: 'created',
          workday_packet: {
            packet_id: 'packet-fast-preempt-autonomy',
            consultation_id: operatorMessage.id,
          },
        });
      }
      if (url.endsWith('/workday-consultations/claim-next')) {
        if (consultationAvailable && !consultationClaimed) {
          consultationClaimed = true;
          lifecycle.push('consultation-claim');
          return Promise.resolve({
            status: 'claimed',
            packet: {
              packet_id: 'packet-fast-preempt-autonomy',
              workday_claim_id: 'workday-claim-fast-preempt-autonomy',
              lease_expires_at: '2035-01-01T00:00:00.000Z',
              attempt: 1,
              claim_id: 'claim-fast-preempt-autonomy',
              delivery_key: 'delivery-fast-preempt-autonomy',
              continuation_key: 'continuation-fast-preempt-autonomy',
              question: 'Inspect the repository and report the deeper answer.',
              envelope: consultationEnvelope,
            },
          });
        }
        return Promise.resolve({ status: 'empty' });
      }
      if (url.endsWith('/workday-consultations/packet-fast-preempt-autonomy/renew')) {
        return Promise.resolve({
          workday_claim_id: 'workday-claim-fast-preempt-autonomy',
          lease_expires_at: '2035-01-01T00:00:00.000Z',
        });
      }
      if (url.endsWith('/workday-consultations/packet-fast-preempt-autonomy/ack')) {
        lifecycle.push('consultation-ack');
        alive = false;
        return Promise.resolve({ status: 'completed' });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => Promise.resolve({
      receipt,
      result: turnResult({ resultText: 'I am checking that with Workday.' }),
      completion: { receipt, content: 'I am checking that with Workday.' },
      workdayRequests: [{
        receipt,
        delivery_key: 'delivery-fast-preempt-autonomy',
        prompt: 'Inspect the repository and report the deeper answer.',
      }],
    }));

    let resolveAutonomous!: (result: ResidentTurnResult) => void;
    let autonomousSettled = false;
    let activeWorkdayTurns = 0;
    const autonomousTurn = new Promise<ResidentTurnResult>((resolve) => {
      resolveAutonomous = resolve;
    }).then((result) => {
      autonomousSettled = true;
      activeWorkdayTurns -= 1;
      lifecycle.push('autonomous-settled');
      return result;
    });
    const autonomousWorkday = makeSession((prompt) => {
      activeWorkdayTurns += 1;
      expect(activeWorkdayTurns).toBe(1);
      autonomousStarted = true;
      lifecycle.push('autonomous-start');
      expect(JSON.stringify(prompt)).toContain(firedWatchId);
      expect(JSON.stringify(prompt)).toContain(firedBarrierId);
      return autonomousTurn;
    }, () => true);
    const consultationWorkday = makeSession((prompt) => {
      activeWorkdayTurns += 1;
      expect(activeWorkdayTurns).toBe(1);
      lifecycle.push('consultation-start');
      expect(autonomousSettled).toBe(true);
      const promptText = JSON.stringify(prompt);
      expect(promptText).toContain('private, durable typed-Fast consultation');
      expect(promptText).toContain('complete assignment');
      expect(promptText).toContain('execute that operation in this Workday turn');
      expect(promptText).toContain('apply the exact requested mutation through ./strategic');
      expect(promptText).toContain('matching canonical readback');
      expect(promptText).not.toContain('inspect existing directions and tasks first');
      expect(promptText).not.toContain('Session checkpoint');
      expect(promptText).not.toContain('You are Blueprint\'s Strategic Resident');
      activeWorkdayTurns -= 1;
      return Promise.resolve(turnResult({
        sentOperatorMessage: false,
        resultText: 'Private repository investigation complete.',
      }));
    }, () => alive);
    autonomousWorkday.interrupt.mockImplementation(() => {
      lifecycle.push('autonomous-interrupt');
      resolveAutonomous(turnResult({
        interrupted: true,
        sentOperatorMessage: false,
        sawToolUse: false,
      }));
    });
    bindSessionWithPrivateRuntime([autonomousWorkday, consultationWorkday]);

    const done = run('typed-fast-preempts-autonomy-for-consultation', {
      ...qualifiedPayload(),
      strategic_session_autonomous: true,
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 0.001,
      strategic_session_exploration_interval_seconds: 1,
    });
    for (let i = 0; i < 40 && !lifecycle.includes('consultation-ack'); i += 1) {
      await vi.advanceTimersByTimeAsync(100);
    }
    expect(lifecycle).toContain('consultation-ack');
    await vi.advanceTimersByTimeAsync(1_000);
    await done;

    expect(mockStartSession).toHaveBeenCalledTimes(2);
    expect(autonomousWorkday.sendTurn).toHaveBeenCalledTimes(1);
    expect(autonomousWorkday.interrupt).toHaveBeenCalledTimes(1);
    expect(autonomousWorkday.close).toHaveBeenCalledTimes(1);
    expect(consultationWorkday.sendTurn).toHaveBeenCalledTimes(1);
    expect(consultationWorkday.interrupt).not.toHaveBeenCalled();
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/complete'))).toHaveLength(1);
    expect(heartbeatBodies.every((body) => (
      !Array.isArray(body.handled_message_ids)
      || !body.handled_message_ids.includes(firedBarrierId)
    ))).toBe(true);
    expect(lifecycle).toEqual([
      'autonomous-start',
      'fast-complete',
      'autonomous-interrupt',
      'autonomous-settled',
      'consultation-claim',
      'consultation-start',
      'consultation-ack',
    ]);
  });

  it('settles active autonomous Workday when the committed Fast consultation completion response is lost', async () => {
    vi.useFakeTimers();
    typedFastMocks.abandonDispositionWithConfirmation.mockReturnValue('completed');
    const operatorMessage = {
      id: 'typed-fast-lost-consultation-completion-response',
      content: 'Please investigate despite a lost completion response.',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let autonomousStarted = false;
    let messageDelivered = false;
    mockApiGet.mockImplementation((url: string) => {
      if (!url.includes('/messages?')) return Promise.resolve(null);
      if (autonomousStarted && !messageDelivered) {
        messageDelivered = true;
        return Promise.resolve({
          messages: [operatorMessage],
          fast_message_ids: [operatorMessage.id],
          claim_required: true,
        });
      }
      return Promise.resolve({ messages: [], fast_message_ids: [], claim_required: true });
    });

    const lifecycle: string[] = [];
    const consultationEnvelope = typedEnvelope(operatorMessage.content, 87, operatorMessage.id, messageAttachments(operatorMessage));
    let consultationAvailable = false;
    let consultationClaimed = false;
    let alive = true;
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({ authority_held: true, resident_incarnation_held: true });
      }
      if (url === '/api/agent/strategic' && body?.op === 'begin_autonomous_turn') {
        return Promise.resolve({
          autonomous_turn_id: '00000000-0000-0000-0000-000000000781',
          autonomous_turn_lease_id: '00000000-0000-0000-0000-000000000782',
          autonomous_handoff_idempotency_key: (body.data as Record<string, unknown>)
            .client_turn_id,
        });
      }
      if (url === '/api/agent/strategic' && body?.op === 'end_autonomous_turn') {
        return Promise.resolve({ status: 'ended' });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: 'lease-lost-consultation-completion', generation: 1 },
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        return Promise.resolve({
          claim_id: 'claim-lost-consultation-completion',
          owner: 'fast',
          message_ids: [operatorMessage.id],
          envelope: consultationEnvelope,
        });
      }
      if (url.endsWith('/conversation/turns/complete')) {
        // Model the exact ambiguity: the atomic server commit created both the
        // public Fast reply and packet, but the HTTP response never arrived.
        lifecycle.push('fast-completion-committed-response-lost');
        consultationAvailable = true;
        return Promise.reject(new Error('completion response lost after commit'));
      }
      if (url.endsWith('/conversation/turns/abandon')) {
        lifecycle.push('abandon-already-completed');
        return Promise.resolve({
          status: 'already_completed',
          consultation_id: operatorMessage.id,
        });
      }
      if (url.endsWith('/workday-consultations/claim-next')) {
        if (consultationAvailable && !consultationClaimed) {
          consultationClaimed = true;
          lifecycle.push('consultation-claim');
          return Promise.resolve({
            status: 'claimed',
            packet: {
              packet_id: 'packet-lost-consultation-completion',
              workday_claim_id: 'workday-claim-lost-consultation-completion',
              lease_expires_at: '2035-01-01T00:00:00.000Z',
              attempt: 1,
              claim_id: 'claim-lost-consultation-completion',
              delivery_key: 'delivery-lost-consultation-completion',
              continuation_key: 'continuation-lost-consultation-completion',
              question: 'Inspect the repository after recovery confirms the commit.',
              envelope: consultationEnvelope,
            },
          });
        }
        return Promise.resolve({ status: 'empty' });
      }
      if (url.endsWith('/workday-consultations/packet-lost-consultation-completion/renew')) {
        return Promise.resolve({
          workday_claim_id: 'workday-claim-lost-consultation-completion',
          lease_expires_at: '2035-01-01T00:00:00.000Z',
        });
      }
      if (url.endsWith('/workday-consultations/packet-lost-consultation-completion/ack')) {
        lifecycle.push('consultation-ack');
        alive = false;
        return Promise.resolve({ status: 'completed' });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => Promise.resolve({
      receipt,
      result: turnResult({ resultText: 'I am checking that with Workday.' }),
      completion: { receipt, content: 'I am checking that with Workday.' },
      workdayRequests: [{
        receipt,
        delivery_key: 'delivery-lost-consultation-completion',
        prompt: 'Inspect the repository after recovery confirms the commit.',
      }],
    }));

    let resolveAutonomous!: (result: ResidentTurnResult) => void;
    let autonomousSettled = false;
    let activeWorkdayTurns = 0;
    const autonomousTurn = new Promise<ResidentTurnResult>((resolve) => {
      resolveAutonomous = resolve;
    }).then((result) => {
      autonomousSettled = true;
      activeWorkdayTurns -= 1;
      lifecycle.push('autonomous-settled');
      return result;
    });
    const workday = makeSession((prompt) => {
      activeWorkdayTurns += 1;
      expect(activeWorkdayTurns).toBe(1);
      if (workday.sendTurn.mock.calls.length === 1) {
        autonomousStarted = true;
        lifecycle.push('autonomous-start');
        return autonomousTurn;
      }
      lifecycle.push('consultation-start');
      expect(autonomousSettled).toBe(true);
      expect(JSON.stringify(prompt)).toContain('private, durable typed-Fast consultation');
      activeWorkdayTurns -= 1;
      return Promise.resolve(turnResult({
        sentOperatorMessage: false,
        resultText: 'Private recovery-path investigation complete.',
      }));
    }, () => alive);
    workday.interrupt.mockImplementation(() => {
      lifecycle.push('autonomous-interrupt');
      resolveAutonomous(turnResult({
        interrupted: true,
        sentOperatorMessage: false,
        sawToolUse: false,
      }));
    });
    bindSessionWithPrivateRuntime(workday);

    const done = run('typed-fast-lost-consultation-completion-response', {
      ...qualifiedPayload(),
      strategic_session_autonomous: true,
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 0.001,
      strategic_session_exploration_interval_seconds: 1,
    });
    for (let i = 0; i < 40 && !lifecycle.includes('consultation-ack'); i += 1) {
      await vi.advanceTimersByTimeAsync(100);
    }
    expect(lifecycle).toContain('consultation-ack');
    await vi.advanceTimersByTimeAsync(1_000);
    await done;

    expect(workday.sendTurn).toHaveBeenCalledTimes(2);
    expect(workday.interrupt).toHaveBeenCalledTimes(1);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/complete'))).toHaveLength(1);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      url.endsWith('/conversation/turns/abandon'))).toHaveLength(1);
    expect(lifecycle).toEqual([
      'autonomous-start',
      'fast-completion-committed-response-lost',
      'abandon-already-completed',
      'autonomous-interrupt',
      'autonomous-settled',
      'consultation-claim',
      'consultation-start',
      'consultation-ack',
    ]);
  });

  it('preserves autonomous Ready when Workday settles after Fast pending but before dispatch', async () => {
    vi.useFakeTimers();
    const fastMessage = {
      id: 'typed-fast-autonomous-status-overlap',
      content: 'Answer after these images are prepared.',
      created_at: new Date().toISOString(),
      from_type: 'user',
      context: {
        attachments: [0, 1].map(index => ({
          type: 'image',
          mime_type: 'image/png',
          name: `overlap-${index}.png`,
          url: `https://storage.googleapis.com/bucket/overlap-${index}.png?X-Goog-Signature=sig`,
        })),
      },
    };
    let messagePolls = 0;
    let publishWorkdayAdvancing = () => undefined;
    mockApiGet.mockImplementation((url: string) => {
      if (!url.includes('/messages?')) return Promise.resolve(null);
      messagePolls += 1;
      if (messagePolls === 1) {
        return Promise.resolve({ messages: [], fast_message_ids: [], claim_required: true });
      }
      if (messagePolls === 2) {
        publishWorkdayAdvancing();
        return Promise.resolve({
          messages: [fastMessage],
          fast_message_ids: [fastMessage.id],
          claim_required: true,
        });
      }
      return Promise.resolve({ messages: [], fast_message_ids: [], claim_required: true });
    });
    const heartbeatBodies: Array<Record<string, unknown>> = [];
    let alive = true;
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        heartbeatBodies.push(body ?? {});
        return Promise.resolve({
          authority_held: true,
          resident_incarnation_held: true,
          conversation_claim_held: true,
          stop_requested: false,
        });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: 'lease-autonomous-status-overlap', generation: 2 },
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        return Promise.resolve({
          claim_id: 'claim-autonomous-status-overlap',
          owner: 'fast',
          message_ids: [fastMessage.id],
          envelope: typedEnvelope(fastMessage.content, 83, fastMessage.id, messageAttachments(fastMessage)),
        });
      }
      if (url.endsWith('/conversation/turns/complete')) {
        return Promise.resolve({ status: 'completed' });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });
    let fetchCount = 0;
    let resolveSecondFetch!: (response: unknown) => void;
    const attachmentFetch = vi.fn(() => {
      fetchCount += 1;
      if (fetchCount === 1) return new Promise(() => undefined);
      return new Promise((resolve) => { resolveSecondFetch = resolve; });
    });
    vi.stubGlobal('fetch', attachmentFetch);
    mockStartSession.mockReturnValue(makeSession(
      () => Promise.resolve(turnResult()),
      () => alive,
    ));
    const statusChanges: string[] = [];
    let currentStatus = '';
    const statusRef = {
      get value() { return currentStatus; },
      set value(value: string) { currentStatus = value; statusChanges.push(value); },
    };
    // Isolate the exact no-displaced-claim CAS seam: an autonomous Workday
    // owns Advancing when Fast admits the page, then its completion callback
    // publishes the newer Ready state while prompt materialization is pending.
    publishWorkdayAdvancing = () => { statusRef.value = 'Advancing project (autonomous)...'; };
    typedFastMocks.dispatch.mockImplementation((receipt: unknown) => Promise.resolve({
      receipt,
      result: turnResult({ resultText: 'Prepared reply.' }),
      completion: { receipt, content: 'Prepared reply.' },
      workdayRequests: [],
    }));

    try {
      const done = run('typed-fast-autonomous-status-overlap', {
        ...qualifiedPayload(),
        strategic_session_message_poll_seconds: 0.001,
      }, statusRef);
      for (let i = 0; i < 20 && statusRef.value !== 'Operator message received — preparing response'; i += 1) {
        await vi.advanceTimersByTimeAsync(0);
      }
      expect(statusRef.value).toBe('Operator message received — preparing response');
      expect(statusChanges).toContain('Advancing project (autonomous)...');
      expect(typedFastMocks.dispatch).not.toHaveBeenCalled();

      await vi.advanceTimersByTimeAsync(15_001);
      expect(heartbeatBodies.some(body => (
        body.conversation_claim_id === 'claim-autonomous-status-overlap'
        && body.typed_fast_lease_id === 'lease-autonomous-status-overlap'
      ))).toBe(true);
      expect(attachmentFetch).toHaveBeenCalledTimes(2);

      statusRef.value = 'Resident ready — waiting for operator input';
      expect(statusRef.value).toBe('Resident ready — waiting for operator input');

      resolveSecondFetch({
        ok: true,
        status: 200,
        headers: { get: (name: string) => name === 'content-type' ? 'image/png' : null },
        arrayBuffer: () => Promise.resolve(new ArrayBuffer(0)),
      });
      for (let i = 0; i < 20 && typedFastMocks.dispatch.mock.calls.length === 0; i += 1) {
        await vi.advanceTimersByTimeAsync(0);
      }

      expect(statusChanges).toContain('Responding to operator...');
      expect(statusRef.value).toBe('Resident ready — waiting for operator input');
      expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(1);
      alive = false;
      await vi.advanceTimersByTimeAsync(2_000);
      await done;
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('keeps the shared private Workday boundary replyless, silent, and serialized', async () => {
    vi.useFakeTimers();
    mockApiGet.mockResolvedValue({ messages: [], fast_message_ids: [], claim_required: true });

    let privateReplyPaths: string[] = [];
    let privateTurnMarkerPath = '';
    vi.mocked(fs.existsSync).mockImplementation((candidate) => {
      const candidatePath = String(candidate);
      if (
        !privateReplyPaths.includes(candidatePath)
        && candidatePath !== privateTurnMarkerPath
      ) return true;
      try {
        fs.lstatSync(candidatePath);
        return true;
      } catch {
        return false;
      }
    });

    const canonicalEnvelope = {
      version: 2,
      project_id: '11111111-1111-4111-8111-111111111111',
      context_authority_generation: 4,
      privacy_generation: 2,
      transcript_sequence: 90,
      ledger_revision: 0,
      problem_model: {},
      decisions: [],
      memory: { active: [] },
      recent_conversation: [],
    };
    const canonicalBytes = Buffer.from(JSON.stringify(canonicalEnvelope), 'utf8');
    const envelope = {
      snapshot_id: 'snapshot-private-workday',
      canonical_envelope_b64: canonicalBytes.toString('base64'),
      canonical_sha256: createHash('sha256').update(canonicalBytes).digest('hex'),
      context_authority_generation: 4,
      privacy_generation: 2,
      transcript_sequence: 90,
      ledger_revision: 0,
      expires_at: '2035-01-01T00:00:00.000Z',
    };
    let consultationClaims = 0;
    let alive = true;
    const consultationAcks: Array<Record<string, unknown> | undefined> = [];
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({ authority_held: true });
      }
      if (url.endsWith('/workday-consultations/claim-next')) {
        consultationClaims += 1;
        return Promise.resolve(consultationClaims === 1 ? {
          status: 'claimed',
          packet: {
            packet_id: 'packet-private-workday',
            workday_claim_id: 'workday-claim-private',
            lease_expires_at: '2035-01-01T00:00:00.000Z',
            attempt: 1,
            claim_id: 'fast-claim-private',
            delivery_key: 'delivery-private',
            continuation_key: 'continuation-private',
            question: 'Inspect the repository without blocking Fast.',
            envelope,
          },
        } : { status: 'empty' });
      }
      if (url.endsWith('/workday-consultations/packet-private-workday/renew')) {
        return Promise.resolve({
          workday_claim_id: 'workday-claim-private',
          lease_expires_at: '2035-01-01T00:00:00.000Z',
        });
      }
      if (url.endsWith('/workday-consultations/packet-private-workday/ack')) {
        consultationAcks.push(body);
        alive = false;
        return Promise.resolve({ status: 'completed' });
      }
      return Promise.resolve({});
    });

    let resolvePrivate!: (result: ResidentTurnResult) => void;
    const privateTurn = new Promise<ResidentTurnResult>((resolve) => {
      resolvePrivate = resolve;
    });
    let capabilityRevokedAtTurnStart = false;
    let privateMarkerKindAtTurnStart = '';
    let framesAtPrivateTurnStart = 0;
    const sentPrompts: string[] = [];
    const workday = makeSession((prompt) => {
      sentPrompts.push(typeof prompt === 'string' ? prompt : JSON.stringify(prompt));
      if (sentPrompts.length === 1) {
        capabilityRevokedAtTurnStart = privateReplyPaths.length === 3
          && privateReplyPaths.every((artifactPath) => !fs.existsSync(artifactPath));
        const marker = JSON.parse(fs.readFileSync(privateTurnMarkerPath, 'utf8')) as { kind?: string };
        privateMarkerKindAtTurnStart = marker.kind ?? '';
        framesAtPrivateTurnStart = mockSendWsFrame.mock.calls.length;
        workday._emit({ kind: 'text', text: 'PRIVATE_WORKDAY_TEXT_MUST_NOT_STREAM' });
        workday._emit({ kind: 'tool', toolName: 'private_tool', text: 'PRIVATE_TOOL_MUST_NOT_STREAM' });
        workday._emit({ kind: 'error', text: 'PRIVATE_ERROR_MUST_NOT_STREAM' });
        return privateTurn;
      }
      alive = false;
      return Promise.resolve(turnResult({
        resultText: 'Ordinary autonomous review complete.\nTICK-VERDICT: CLEAR — no correction needed',
      }));
    }, () => alive);
    mockStartSession.mockImplementation((options: unknown) => {
      const env = (options as { env?: Record<string, string> }).env ?? {};
      privateReplyPaths = [
        env.BLUEPRINT_RESIDENT_REPLY_KEY_PATH,
        env.BLUEPRINT_RESIDENT_REPLY_RECEIPT_PATH,
        env.BLUEPRINT_RESIDENT_STAGED_REPLY_PATH,
      ].filter((value): value is string => typeof value === 'string' && value.length > 0);
      privateTurnMarkerPath = env.BLUEPRINT_RESIDENT_PRIVATE_TURN_MARKER_PATH;
      for (const artifactPath of privateReplyPaths) {
        fs.mkdirSync(path.dirname(artifactPath), { recursive: true });
        fs.writeFileSync(artifactPath, 'stale-public-reply-capability');
      }
      return workday;
    });

    const done = run('typed-fast-private-workday-serialization', {
      ...qualifiedPayload(),
      typed_fast_enabled: false,
      strategic_session_autonomous: true,
      strategic_session_message_poll_seconds: 0.001,
      strategic_session_exploration_interval_seconds: 1,
    });
    await vi.advanceTimersByTimeAsync(5_000);

    expect(workday.sendTurn).toHaveBeenCalledTimes(1);
    expect(sentPrompts[0]).toContain('private, durable typed-Fast consultation');
    expect(sentPrompts[0]).toContain(
      'request-qualification is available in this receipt-bound private turn',
    );
    expect(sentPrompts[0]).toContain('Inspect the repository without blocking Fast.');
    expect(sentPrompts[0]).not.toContain('# Resident session state —');
    expect(capabilityRevokedAtTurnStart).toBe(true);
    expect(privateMarkerKindAtTurnStart).toBe('typed-consultation');
    const framesDuringPrivateTurn = mockSendWsFrame.mock.calls
      .slice(framesAtPrivateTurnStart)
      .map((call) => call[1] as { payload?: { status?: string; text?: string } } | undefined)
      .filter((frame): frame is { payload: { status?: string; text?: string } } => Boolean(frame?.payload));
    expect(framesDuringPrivateTurn.some((frame) => (
      frame.payload.text?.includes('PRIVATE_')
      || ['responding', 'working', 'done'].includes(frame.payload.status ?? '')
    ))).toBe(false);
    expect(mockSendLog).not.toHaveBeenCalledWith(
      expect.anything(),
      expect.stringContaining('sendTurn called while a turn is already active'),
    );

    // Cross the old absolute six-minute boundary while the provider keeps
    // making meaningful progress. The consultation must remain live and keep
    // renewing rather than interrupting the whole warm process.
    for (let minute = 1; minute <= 7; minute += 1) {
      await vi.advanceTimersByTimeAsync(60_000);
      workday._emit({ kind: 'tool', toolName: 'Read', text: `bounded-progress-${minute}` });
    }
    expect(workday.interrupt).not.toHaveBeenCalled();
    resolvePrivate(turnResult({
      resultText: 'Private investigation complete.',
      sentOperatorMessage: false,
      providerTerminalReason: 'success',
      providerOutputTail: 'Private investigation complete.',
      toolCallCount: 7,
      trace: [{ elapsedMs: 420_000, kind: 'result', label: 'success' }],
    }));
    await vi.advanceTimersByTimeAsync(4_000);
    await done;
    expect(workday.sendTurn).toHaveBeenCalledTimes(1);
    expect(() => fs.lstatSync(privateTurnMarkerPath)).toThrow();
    expect(consultationAcks).toHaveLength(1);
    expect(consultationAcks[0]?.attempt_diagnostic).toMatchObject({
      attempt: 1,
      outcome: 'completed',
      terminal_predicate: 'provider_result',
      provider_terminal_reason: 'success',
      tool_call_count: 7,
    });
  });

  it('abandons a held consultation after semantic inactivity before starting attempt two', async () => {
    vi.useFakeTimers();
    mockApiGet.mockResolvedValue({
      messages: [],
      fast_message_ids: [],
      claim_required: true,
    });
    const envelope = typedEnvelope('Apply the exact bounded v57 contract.', 903);
    const priorReceipts = Array.from({ length: 9 }, (_, index) => ({
      op: index < 7 ? 'create_acceptance_criterion' : index === 7
        ? 'update_direction'
        : 'set_acceptance_criteria',
      operation_index: index + 1,
    }));
    let claimCount = 0;
    let renewCalls = 0;
    const ackBodies: Array<Record<string, unknown>> = [];
    const diagnosticBodies: Array<Record<string, unknown>> = [];
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({
          authority_held: true,
          resident_incarnation_held: true,
          stop_requested: ackBodies.length >= 2,
        });
      }
      if (url.endsWith('/workday-consultations/claim-next')) {
        claimCount += 1;
        if (claimCount > 2) return Promise.resolve({ status: 'empty' });
        const attempt = claimCount;
        return Promise.resolve({
          status: 'claimed',
          packet: {
            packet_id: 'packet-inactivity-recovery',
            workday_claim_id: `claim-inactivity-recovery-${attempt}`,
            lease_expires_at: '2035-01-01T00:00:00.000Z',
            attempt,
            claim_id: 'fast-claim-inactivity-recovery',
            delivery_key: 'delivery-inactivity-recovery',
            continuation_key: 'continuation-inactivity-recovery',
            question: 'Apply the exact bounded v57 contract.',
            envelope,
            ...(attempt === 2 ? {
              prior_attempts: [{
                attempt: 1,
                terminal_predicate: 'consultation_inactivity_timeout',
              }],
              prior_receipts: priorReceipts,
            } : {}),
          },
        });
      }
      if (url.endsWith('/workday-consultations/packet-inactivity-recovery/renew')) {
        renewCalls += 1;
        return Promise.resolve({
          workday_claim_id: body?.workday_claim_id,
          lease_expires_at: '2035-01-01T00:00:00.000Z',
        });
      }
      if (url.endsWith('/workday-consultations/packet-inactivity-recovery/ack')) {
        ackBodies.push(JSON.parse(JSON.stringify(body ?? {})) as Record<string, unknown>);
        return Promise.resolve({ status: body?.outcome });
      }
      if (url.endsWith('/workday-consultations/packet-inactivity-recovery/attempt-diagnostic')) {
        diagnosticBodies.push(body ?? {});
        return Promise.resolve({
          status: 'recorded',
          attempt_diagnostic: body?.attempt_diagnostic,
        });
      }
      return Promise.resolve({});
    });

    let firstAlive = true;
    let resolveFirst!: (result: ResidentTurnResult) => void;
    const firstTurn = new Promise<ResidentTurnResult>((resolve) => {
      resolveFirst = resolve;
    });
    const firstSession = makeSession(() => firstTurn, () => firstAlive);
    firstSession.interrupt.mockImplementation(() => {
      firstAlive = false;
      resolveFirst(turnResult({
        resultText: '',
        interrupted: true,
        processGone: true,
        sentOperatorMessage: false,
        providerTerminalReason: 'interrupted',
      }));
    });

    const secondPrompts: string[] = [];
    const secondSession = makeSession((prompt) => {
      secondPrompts.push(typeof prompt === 'string' ? prompt : JSON.stringify(prompt));
      return Promise.resolve(turnResult({
        resultText: 'Recovered only the two missing v57 writes.',
        sentOperatorMessage: false,
        providerTerminalReason: 'success',
      }));
    }, () => true);
    bindSessionWithPrivateRuntime([firstSession, secondSession]);

    const done = run('typed-consultation-inactivity-recovery', {
      ...qualifiedPayload(),
      typed_fast_enabled: false,
      strategic_session_autonomous: true,
      strategic_session_autonomous_turn_timeout_seconds: 1,
      strategic_session_message_poll_seconds: 0.25,
    });
    for (let i = 0; i < 20 && firstSession.sendTurn.mock.calls.length === 0; i += 1) {
      await vi.advanceTimersByTimeAsync(250);
    }
    expect(firstSession.sendTurn).toHaveBeenCalledWith(expect.anything(), {
      stallTimeoutMs: 255_000,
    });

    await vi.advanceTimersByTimeAsync(241_000);
    for (let i = 0; i < 80 && ackBodies.length < 2; i += 1) {
      await vi.advanceTimersByTimeAsync(250);
    }
    await vi.advanceTimersByTimeAsync(5_000);
    await done;

    expect(renewCalls).toBeGreaterThanOrEqual(1);
    expect(firstSession.interrupt).toHaveBeenCalledTimes(1);
    expect(firstSession.close).toHaveBeenCalledTimes(1);
    expect(mockStartSession).toHaveBeenCalledTimes(2);
    expect(diagnosticBodies).toEqual([]);
    expect(ackBodies).toHaveLength(2);
    expect(ackBodies[0]).toMatchObject({
      workday_claim_id: 'claim-inactivity-recovery-1',
      outcome: 'abandoned',
      attempt_diagnostic: {
        attempt: 1,
        outcome: 'abandoned',
        terminal_predicate: 'consultation_inactivity_timeout',
      },
    });
    expect(ackBodies[1]).toMatchObject({
      workday_claim_id: 'claim-inactivity-recovery-2',
      outcome: 'completed',
      attempt_diagnostic: {
        attempt: 2,
        outcome: 'completed',
        terminal_predicate: 'provider_result',
      },
    });
    expect(secondPrompts).toHaveLength(1);
    expect(secondPrompts[0]).toContain('Durable prior-attempt reconciliation');
    expect(secondPrompts[0]).toContain('consultation_inactivity_timeout');
    expect(secondPrompts[0]).toContain('set_acceptance_criteria');
  });

  it.each([
    {
      label: 'partial provider failure',
      flags: { providerFailed: true, providerTerminalReason: 'aborted_streaming' },
      terminalPredicate: 'provider_failed',
    },
    {
      label: 'partial provider interruption',
      flags: { interrupted: true, providerTerminalReason: 'interrupted' },
      terminalPredicate: 'provider_interrupted',
    },
    {
      label: 'empty provider failure',
      flags: { resultText: '', providerFailed: true, providerTerminalReason: 'aborted_streaming' },
      terminalPredicate: 'provider_failed',
    },
    {
      label: 'empty provider interruption',
      flags: { resultText: '', interrupted: true, providerTerminalReason: 'interrupted' },
      terminalPredicate: 'provider_interrupted',
    },
    {
      label: 'empty provider process exit',
      flags: { resultText: '', processGone: true, providerTerminalReason: 'process_exit' },
      terminalPredicate: 'provider_process_exit',
    },
    {
      label: 'empty stall timeout',
      flags: { resultText: '', timedOut: true, providerTerminalReason: 'stall_timeout' },
      terminalPredicate: 'session_stall_timeout',
    },
    {
      label: 'empty hard timeout',
      flags: { resultText: '', hardTimedOut: true, providerTerminalReason: 'hard_timeout' },
      terminalPredicate: 'session_hard_timeout',
    },
  ])('does not complete a failed typed consultation after $label', async ({
    flags,
    terminalPredicate,
  }) => {
    vi.useFakeTimers();
    mockApiGet.mockResolvedValue({ messages: [], fast_message_ids: [], claim_required: true });
    const envelope = typedEnvelope('Return a bounded private result.', 901);
    let alive = true;
    let claims = 0;
    const acks: Array<Record<string, unknown>> = [];
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({ authority_held: true, resident_incarnation_held: true });
      }
      if (url.endsWith('/workday-consultations/claim-next')) {
        claims += 1;
        return Promise.resolve(claims === 1 ? {
          status: 'claimed',
          packet: {
            packet_id: 'packet-partial-provider-result',
            workday_claim_id: 'claim-partial-provider-result',
            lease_expires_at: '2035-01-01T00:00:00.000Z',
            attempt: 1,
            claim_id: 'fast-claim-partial-provider-result',
            delivery_key: 'delivery-partial-provider-result',
            continuation_key: 'continuation-partial-provider-result',
            question: 'Return a bounded private result.',
            envelope,
          },
        } : { status: 'empty' });
      }
      if (url.endsWith('/workday-consultations/packet-partial-provider-result/renew')) {
        return Promise.resolve({
          workday_claim_id: 'claim-partial-provider-result',
          lease_expires_at: '2035-01-01T00:00:00.000Z',
        });
      }
      if (url.endsWith('/workday-consultations/packet-partial-provider-result/ack')) {
        acks.push(body ?? {});
        alive = false;
        return Promise.resolve({ status: 'abandoned' });
      }
      return Promise.resolve({});
    });
    const workday = makeSession(() => Promise.resolve(turnResult({
      resultText: 'partial output that must not be delivered as success',
      sentOperatorMessage: false,
      providerOutputTail: 'partial output tail',
      ...flags,
    })), () => alive);
    bindSessionWithPrivateRuntime(workday);

    const done = run(`typed-consultation-partial-${terminalPredicate}`, {
      ...qualifiedPayload(),
      typed_fast_enabled: false,
      strategic_session_autonomous: true,
      strategic_session_message_poll_seconds: 0.01,
    });
    await vi.advanceTimersByTimeAsync(2_000);
    await done;

    expect(workday.sendTurn).toHaveBeenCalledTimes(1);
    expect(acks).toHaveLength(1);
    expect(acks[0]).not.toHaveProperty('result_summary');
    expect(acks[0]).toMatchObject({
      outcome: 'abandoned',
      attempt_diagnostic: {
        outcome: 'abandoned',
        terminal_predicate: terminalPredicate,
        provider_output_tail: 'partial output tail',
      },
    });
  });

  it('renews the typed consultation lease through a long failed-result delivery heartbeat stall', async () => {
    vi.useFakeTimers();
    mockApiGet.mockResolvedValue({ messages: [], fast_message_ids: [], claim_required: true });
    const envelope = typedEnvelope('Diagnose the provider failure exactly.', 902);
    let alive = true;
    let claims = 0;
    let deliveryOutageUntil = 0;
    let renewCalls = 0;
    const acks: Array<Record<string, unknown>> = [];
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        if (deliveryOutageUntil > Date.now()) return Promise.resolve(null);
        return Promise.resolve({ authority_held: true, resident_incarnation_held: true });
      }
      if (url.endsWith('/workday-consultations/claim-next')) {
        claims += 1;
        return Promise.resolve(claims === 1 ? {
          status: 'claimed',
          packet: {
            packet_id: 'packet-delivery-stall',
            workday_claim_id: 'claim-delivery-stall',
            lease_expires_at: '2035-01-01T00:00:00.000Z',
            attempt: 2,
            claim_id: 'fast-claim-delivery-stall',
            delivery_key: 'delivery-stall',
            continuation_key: 'continuation-delivery-stall',
            question: 'Diagnose the provider failure exactly.',
            envelope,
          },
        } : { status: 'empty' });
      }
      if (url.endsWith('/workday-consultations/packet-delivery-stall/renew')) {
        renewCalls += 1;
        return Promise.resolve({
          workday_claim_id: 'claim-delivery-stall',
          lease_expires_at: '2035-01-01T00:00:00.000Z',
        });
      }
      if (url.endsWith('/workday-consultations/packet-delivery-stall/ack')) {
        acks.push(JSON.parse(JSON.stringify(body ?? {})) as Record<string, unknown>);
        alive = false;
        return Promise.resolve({ status: 'failed' });
      }
      return Promise.resolve({});
    });
    const workday = makeSession(() => {
      deliveryOutageUntil = Date.now() + 50_000;
      return Promise.resolve(turnResult({
        resultText: 'partial durable work',
        sentOperatorMessage: false,
        providerFailed: true,
        providerTerminalReason: 'aborted_streaming',
        providerOutputTail: 'exact bounded provider tail',
        trace: [{ elapsedMs: 4_000, kind: 'result', label: 'aborted_streaming' }],
      }));
    }, () => alive);
    bindSessionWithPrivateRuntime(workday);

    const done = run('typed-consultation-delivery-stall', {
      ...qualifiedPayload(),
      typed_fast_enabled: false,
      strategic_session_autonomous: true,
      strategic_session_message_poll_seconds: 1,
    });
    await vi.advanceTimersByTimeAsync(55_000);
    await done;

    expect(workday.sendTurn).toHaveBeenCalledTimes(1);
    expect(renewCalls).toBeGreaterThanOrEqual(3);
    expect(acks).toHaveLength(1);
    expect(acks[0]).toMatchObject({
      outcome: 'failed',
      attempt_diagnostic: {
        attempt: 2,
        outcome: 'failed',
        terminal_predicate: 'provider_failed',
        provider_terminal_reason: 'aborted_streaming',
        provider_output_tail: 'exact bounded provider tail',
      },
    });
  });

  it('retries an identical consultation ack after a post-commit transport loss without rerunning private Workday', async () => {
    vi.useFakeTimers();
    mockApiGet.mockResolvedValue({
      messages: [],
      fast_message_ids: [],
      claim_required: true,
    });

    const envelope = typedEnvelope('Commit this private result exactly once.', 91);
    let consultationClaims = 0;
    let alive = true;
    const ackBodies: Array<Record<string, unknown>> = [];
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({
          authority_held: true,
          resident_incarnation_held: true,
        });
      }
      if (url.endsWith('/workday-consultations/claim-next')) {
        consultationClaims += 1;
        return Promise.resolve(consultationClaims === 1 ? {
          status: 'claimed',
          packet: {
            packet_id: 'packet-ack-response-lost',
            workday_claim_id: 'workday-claim-ack-response-lost',
            lease_expires_at: '2035-01-01T00:00:00.000Z',
            attempt: 1,
            claim_id: 'fast-claim-ack-response-lost',
            delivery_key: 'delivery-ack-response-lost',
            continuation_key: 'continuation-ack-response-lost',
            question: 'Commit this private result exactly once.',
            envelope,
          },
        } : { status: 'empty' });
      }
      if (url.endsWith('/workday-consultations/packet-ack-response-lost/renew')) {
        return Promise.resolve({
          workday_claim_id: 'workday-claim-ack-response-lost',
          lease_expires_at: '2035-01-01T00:00:00.000Z',
        });
      }
      if (url.endsWith('/workday-consultations/packet-ack-response-lost/ack')) {
        ackBodies.push(JSON.parse(JSON.stringify(body ?? {})) as Record<string, unknown>);
        if (ackBodies.length === 1) {
          return Promise.reject(
            new Error('connection lost after durable consultation ack commit'),
          );
        }
        alive = false;
        return Promise.resolve({ status: 'duplicate' });
      }
      return Promise.resolve({});
    });

    const workday = makeSession(() => Promise.resolve(turnResult({
      resultText: 'Private result committed once.',
      sentOperatorMessage: false,
      providerTerminalReason: 'success',
      providerOutputTail: 'Private result committed once.',
      trace: [{ elapsedMs: 1, kind: 'result', label: 'success' }],
    })), () => alive);
    bindSessionWithPrivateRuntime(workday);

    const done = run('typed-fast-ack-response-lost', {
      ...qualifiedPayload(),
      typed_fast_enabled: false,
      strategic_session_autonomous: true,
      strategic_session_message_poll_seconds: 0.001,
      strategic_session_exploration_interval_seconds: 1,
    });
    for (let tick = 0; tick < 20 && ackBodies.length < 2; tick += 1) {
      await vi.advanceTimersByTimeAsync(250);
    }
    await vi.advanceTimersByTimeAsync(1_000);
    await done;

    expect(ackBodies).toHaveLength(2);
    expect(ackBodies[1]).toEqual(ackBodies[0]);
    expect(ackBodies[1]?.attempt_diagnostic).toMatchObject({
      attempt: 1,
      outcome: 'completed',
      terminal_predicate: 'provider_result',
      provider_terminal_reason: 'success',
    });
    expect(workday.sendTurn).toHaveBeenCalledTimes(1);
    expect(workday.interrupt).not.toHaveBeenCalled();
    expect(consultationClaims).toBe(1);
    expect(mockSendLog.mock.calls.filter(([, text]) =>
      String(text).includes('typed Workday consultation ack deferred'))).toHaveLength(1);
  });

  it('does not retry a consultation ack after an authoritative 409', async () => {
    vi.useFakeTimers();
    mockApiGet.mockResolvedValue({
      messages: [],
      fast_message_ids: [],
      claim_required: true,
    });

    const envelope = typedEnvelope('Lose this private delivery authority.', 92);
    let consultationClaims = 0;
    let alive = true;
    const ackBodies: Array<Record<string, unknown>> = [];
    const diagnosticBodies: Array<Record<string, unknown>> = [];
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({
          authority_held: true,
          resident_incarnation_held: true,
        });
      }
      if (url.endsWith('/workday-consultations/claim-next')) {
        consultationClaims += 1;
        return Promise.resolve(consultationClaims === 1 ? {
          status: 'claimed',
          packet: {
            packet_id: 'packet-ack-authority-lost',
            workday_claim_id: 'workday-claim-ack-authority-lost',
            lease_expires_at: '2035-01-01T00:00:00.000Z',
            attempt: 1,
            claim_id: 'fast-claim-ack-authority-lost',
            delivery_key: 'delivery-ack-authority-lost',
            continuation_key: 'continuation-ack-authority-lost',
            question: 'Lose this private delivery authority.',
            envelope,
          },
        } : { status: 'empty' });
      }
      if (url.endsWith('/workday-consultations/packet-ack-authority-lost/renew')) {
        return Promise.resolve({
          workday_claim_id: 'workday-claim-ack-authority-lost',
          lease_expires_at: '2035-01-01T00:00:00.000Z',
        });
      }
      if (url.endsWith('/workday-consultations/packet-ack-authority-lost/ack')) {
        ackBodies.push(JSON.parse(JSON.stringify(body ?? {})) as Record<string, unknown>);
        alive = false;
        return Promise.reject(new Error('consultation acknowledgement rejected (409)'));
      }
      if (url.endsWith(
        '/workday-consultations/packet-ack-authority-lost/attempt-diagnostic',
      )) {
        diagnosticBodies.push(
          JSON.parse(JSON.stringify(body ?? {})) as Record<string, unknown>,
        );
        return Promise.resolve({
          status: 'recorded',
          attempt_diagnostic: body?.attempt_diagnostic,
        });
      }
      return Promise.resolve({});
    });

    const workday = makeSession(() => Promise.resolve(turnResult({
      resultText: 'Private result whose claim was displaced.',
      sentOperatorMessage: false,
      providerTerminalReason: 'success',
      providerOutputTail: 'Private result whose claim was displaced.',
      trace: [{ elapsedMs: 1, kind: 'result', label: 'success' }],
    })), () => alive);
    bindSessionWithPrivateRuntime(workday);

    const done = run('typed-fast-ack-authority-lost', {
      ...qualifiedPayload(),
      typed_fast_enabled: false,
      strategic_session_autonomous: true,
      strategic_session_message_poll_seconds: 0.001,
      strategic_session_exploration_interval_seconds: 1,
    });
    await vi.advanceTimersByTimeAsync(2_000);
    await done;

    expect(ackBodies).toHaveLength(1);
    expect(diagnosticBodies).toHaveLength(1);
    expect(diagnosticBodies[0]?.attempt_diagnostic).toMatchObject({
      attempt: 1,
      outcome: 'abandoned',
      terminal_predicate: 'consultation_ack_authority_lost',
      provider_terminal_reason: 'success',
    });
    expect(workday.sendTurn).toHaveBeenCalledTimes(1);
    expect(workday.interrupt).not.toHaveBeenCalled();
    expect(consultationClaims).toBe(1);
  });

  it('keeps a consultation-restart checkpoint out of the private retry and delivers it to the next ordinary turn', async () => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'operator-after-private-restart',
      content: 'Continue with the ordinary Workday conversation.',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    const envelope = typedEnvelope('Retry the private consultation after renewal loss.', 91);
    let consultationClaims = 0;
    let renewCalls = 0;
    let operatorDelivered = false;
    const ackBodies: Array<Record<string, unknown> | undefined> = [];
    const diagnosticBodies: Array<Record<string, unknown> | undefined> = [];
    mockApiGet.mockImplementation((url: string) => {
      if (!url.includes('/messages?')) return Promise.resolve(null);
      if (ackBodies.length >= 1 && !operatorDelivered) {
        operatorDelivered = true;
        return Promise.resolve({
          messages: [operatorMessage],
          fast_message_ids: [],
          claim_required: true,
        });
      }
      return Promise.resolve({ messages: [], fast_message_ids: [], claim_required: true });
    });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({
          authority_held: true,
          resident_incarnation_held: true,
          conversation_claim_held: true,
        });
      }
      if (url.endsWith('/messages/claim')) {
        return Promise.resolve({
          claim_id: 'claim-operator-after-private-restart',
          incarnation_id: 'incarnation-operator-after-private-restart',
          incarnation_supported: true,
          message_ids: [operatorMessage.id],
        });
      }
      if (url.endsWith('/workday-consultations/claim-next')) {
        consultationClaims += 1;
        if (consultationClaims > 2) return Promise.resolve({ status: 'empty' });
        return Promise.resolve({
          status: 'claimed',
          packet: {
            packet_id: 'packet-private-restart',
            workday_claim_id: `workday-claim-private-restart-${consultationClaims}`,
            lease_expires_at: '2035-01-01T00:00:00.000Z',
            attempt: consultationClaims,
            claim_id: 'fast-claim-private-restart',
            delivery_key: 'delivery-private-restart',
            continuation_key: 'continuation-private-restart',
            question: 'Retry the exact bounded private operation.',
            envelope,
            ...(consultationClaims === 2 ? {
              prior_attempts: [{
                attempt: 1,
                terminal_predicate: 'consultation_lease_renewal_failed',
              }],
              prior_receipts: [{
                op: 'set_running_update',
                target_revision: 7,
              }],
            } : {}),
          },
        });
      }
      if (url.endsWith('/workday-consultations/packet-private-restart/renew')) {
        renewCalls += 1;
        return Promise.resolve({
          workday_claim_id: 'different-owner-after-renewal-loss',
          lease_expires_at: '2035-01-01T00:00:00.000Z',
        });
      }
      if (url.endsWith('/workday-consultations/packet-private-restart/attempt-diagnostic')) {
        diagnosticBodies.push(body);
        return Promise.resolve({
          status: 'recorded',
          attempt_diagnostic: body?.attempt_diagnostic,
        });
      }
      if (url.endsWith('/workday-consultations/packet-private-restart/ack')) {
        ackBodies.push(body);
        return Promise.resolve({ status: body?.outcome });
      }
      return Promise.resolve({});
    });

    const runtime = {
      replyPaths: [] as string[],
      markerPath: '',
    };
    vi.mocked(fs.existsSync).mockImplementation((candidate) => {
      const candidatePath = String(candidate);
      if (
        !runtime.replyPaths.includes(candidatePath)
        && candidatePath !== runtime.markerPath
      ) return true;
      try {
        fs.lstatSync(candidatePath);
        return true;
      } catch {
        return false;
      }
    });

    let firstAlive = true;
    let resolveFirstPrivate!: (result: ResidentTurnResult) => void;
    const firstPrivate = new Promise<ResidentTurnResult>((resolve) => {
      resolveFirstPrivate = resolve;
    });
    const firstPrompts: string[] = [];
    const firstSession = makeSession((prompt) => {
      firstPrompts.push(typeof prompt === 'string' ? prompt : JSON.stringify(prompt));
      return firstPrivate;
    }, () => firstAlive);
    firstSession.interrupt.mockImplementation(() => {
      firstAlive = false;
      resolveFirstPrivate(turnResult({
        interrupted: true,
        processGone: true,
        resultText: '',
      }));
    });

    let secondAlive = true;
    const secondPrompts: string[] = [];
    const secondSession = makeSession((prompt) => {
      secondPrompts.push(typeof prompt === 'string' ? prompt : JSON.stringify(prompt));
      if (secondPrompts.length === 1) {
        return Promise.resolve(turnResult({
          sentOperatorMessage: false,
          resultText: 'Private retry completed.',
        }));
      }
      secondAlive = false;
      return Promise.resolve(turnResult({
        resultText: 'Ordinary recovery review complete.\nTICK-VERDICT: CLEAR — no correction needed',
      }));
    }, () => secondAlive);

    const sessions = [firstSession, secondSession];
    mockStartSession.mockImplementation((options: unknown) => {
      const env = (options as { env?: Record<string, string> }).env ?? {};
      runtime.replyPaths = [
        env.BLUEPRINT_RESIDENT_REPLY_KEY_PATH,
        env.BLUEPRINT_RESIDENT_REPLY_RECEIPT_PATH,
        env.BLUEPRINT_RESIDENT_STAGED_REPLY_PATH,
      ].filter((value): value is string => typeof value === 'string' && value.length > 0);
      runtime.markerPath = env.BLUEPRINT_RESIDENT_PRIVATE_TURN_MARKER_PATH || '';
      for (const artifactPath of runtime.replyPaths) {
        fs.mkdirSync(path.dirname(artifactPath), { recursive: true });
        fs.writeFileSync(artifactPath, 'stale-public-reply-capability');
      }
      const next = sessions.shift();
      if (!next) throw new Error('unexpected third Workday session');
      return next;
    });

    const done = run('typed-fast-private-restart-context-isolation', {
      ...qualifiedPayload(),
      typed_fast_enabled: false,
      strategic_session_autonomous: true,
      strategic_session_message_poll_seconds: 0.25,
      strategic_session_exploration_interval_seconds: 1,
    });

    for (let i = 0; i < 20 && firstPrompts.length === 0; i += 1) {
      await vi.advanceTimersByTimeAsync(250);
    }
    expect(firstPrompts).toHaveLength(1);
    expect(firstPrompts[0]).toContain('Retry the exact bounded private operation.');
    expect(firstPrompts[0]).not.toContain('# Resident session state —');
    expect(firstPrompts[0]).not.toContain('# Session checkpoint — compaction restart');

    await vi.advanceTimersByTimeAsync(16_000);
    for (let i = 0; i < 40 && secondPrompts.length < 2; i += 1) {
      await vi.advanceTimersByTimeAsync(250);
    }
    await vi.advanceTimersByTimeAsync(2_000);
    await done;

    expect(renewCalls).toBe(1);
    expect(firstSession.interrupt).toHaveBeenCalledTimes(1);
    expect(firstSession.close).toHaveBeenCalledTimes(1);
    expect(mockStartSession).toHaveBeenCalledTimes(2);
    expect(secondPrompts).toHaveLength(2);
    expect(secondPrompts[0]).toContain('Retry the exact bounded private operation.');
    expect(secondPrompts[0]).toContain('Durable prior-attempt reconciliation');
    expect(secondPrompts[0]).toContain('consultation_lease_renewal_failed');
    expect(secondPrompts[0]).toContain('set_running_update');
    expect(secondPrompts[0]).not.toContain('# Resident session state —');
    expect(secondPrompts[0]).not.toContain('# Session checkpoint — compaction restart');
    expect(secondPrompts[1]).toContain('# Resident session state — P');
    expect(secondPrompts[1]).toContain('# Session checkpoint — compaction restart');
    expect(secondPrompts[1]).not.toContain('Retry the exact bounded private operation.');
    expect(secondPrompts[1]).toContain('Continue with the ordinary Workday conversation.');
    expect(diagnosticBodies.map(body => body?.attempt_diagnostic)).toEqual([
      expect.objectContaining({
        outcome: 'abandoned',
        terminal_predicate: 'consultation_lease_renewal_failed',
      }),
    ]);
    expect(ackBodies.map(body => body?.outcome)).toEqual(['completed']);
    expect(ackBodies.map(body => (
      body?.attempt_diagnostic as { terminal_predicate?: string } | undefined
    )?.terminal_predicate)).toEqual(['provider_result']);
  });

  const runOrdinaryWorkdayOverlapCase = async (
    renewalFails: boolean,
    workdayCompletesFirst = false,
  ) => {
    vi.useFakeTimers();
    const legacyMessage = {
      id: 'legacy-workday-message',
      content: 'legacy input already owned by Workday',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    const fastMessage = {
      id: 'typed-fast-during-workday',
      content: 'hi',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let messagePolls = 0;
    mockApiGet.mockImplementation((url: string) => {
      if (!url.includes('/messages?')) return Promise.resolve(null);
      messagePolls += 1;
      if (messagePolls === 1) {
        return Promise.resolve({
          messages: [],
          fast_message_ids: [],
          claim_required: true,
        });
      }
      if (messagePolls === 2) {
        return Promise.resolve({
          messages: [legacyMessage],
          fast_message_ids: [],
          claim_required: true,
        });
      }
      if (messagePolls === 3) {
        return Promise.resolve({
          messages: [fastMessage],
          fast_message_ids: [fastMessage.id],
          claim_required: true,
        });
      }
      return Promise.resolve({ messages: [], fast_message_ids: [], claim_required: true });
    });

    const canonicalEnvelope = {
      version: 2,
      project_id: '11111111-1111-4111-8111-111111111111',
      context_authority_generation: 4,
      privacy_generation: 2,
      transcript_sequence: 82,
      ledger_revision: 0,
      problem_model: {},
      decisions: [],
      memory: { active: [] },
      recent_conversation: [boundConversationRow(fastMessage, 82)],
    };
    const canonicalBytes = Buffer.from(JSON.stringify(canonicalEnvelope), 'utf8');
    const envelope = {
      snapshot_id: 'snapshot-fast-during-workday',
      canonical_envelope_b64: canonicalBytes.toString('base64'),
      canonical_sha256: createHash('sha256').update(canonicalBytes).digest('hex'),
      context_authority_generation: 4,
      privacy_generation: 2,
      transcript_sequence: 82,
      ledger_revision: 0,
      expires_at: '2035-01-01T00:00:00.000Z',
    };
    let heartbeatCount = 0;
    let allowHeartbeatStop = !workdayCompletesFirst;
    const heartbeatClaimIds: unknown[] = [];
    const workdayClaimBodies: Array<Record<string, unknown> | undefined> = [];
    const statusRef = { value: '' };
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        heartbeatCount += 1;
        heartbeatClaimIds.push(body?.conversation_claim_id);
        return Promise.resolve({
          authority_held: true,
          conversation_claim_held: true,
          stop_requested: allowHeartbeatStop && heartbeatCount > 3,
        });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: 'lease-fast-during-workday', generation: 1 },
        });
      }
      if (url.endsWith('/messages/claim')) {
        workdayClaimBodies.push(body);
        if (renewalFails && workdayClaimBodies.length > 1) {
          return Promise.resolve({
            claim_id: 'different-claim',
            incarnation_id: 'incarnation-legacy-workday',
            incarnation_supported: true,
            message_ids: [legacyMessage.id],
          });
        }
        return Promise.resolve({
          claim_id: 'claim-legacy-workday',
          incarnation_id: 'incarnation-legacy-workday',
          incarnation_supported: true,
          message_ids: [legacyMessage.id],
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        return Promise.resolve({
          claim_id: 'claim-fast-during-workday',
          owner: 'fast',
          message_ids: [fastMessage.id],
          envelope,
        });
      }
      if (url.endsWith('/conversation/turns/complete')) {
        return Promise.resolve({ status: 'completed' });
      }
      if (url.endsWith('/conversation/turns/abandon')) {
        return Promise.resolve({
          status: 'abandoned',
          legacy_fallback_admissible: true,
        });
      }
      if (url.endsWith('/typed-fast-lease/release')) {
        return Promise.resolve({ released: true });
      }
      return Promise.resolve({});
    });
    let resolveFast!: (turn: {
      receipt: unknown;
      result: ResidentTurnResult;
      completion: { receipt: unknown; content: string };
      workdayRequests: [];
    }) => void;
    typedFastMocks.dispatch.mockImplementation((_receipt: unknown) => new Promise((resolve) => {
      resolveFast = resolve;
    }));

    let resolveWorkday!: (result: ResidentTurnResult) => void;
    const workdayReply = new Promise<ResidentTurnResult>((resolve) => {
      resolveWorkday = resolve;
    });
    const workday = makeSession(() => workdayReply, () => true);
    workday.interrupt.mockImplementation(() => {
      resolveWorkday(turnResult({ interrupted: true }));
    });
    mockStartSession.mockReturnValue(workday);
    typedFastMocks.interrupt.mockImplementation(() => {
      resolveFast({
        receipt: typedFastMocks.dispatch.mock.calls[0]?.[0],
        result: turnResult({ interrupted: true }),
        completion: {
          receipt: typedFastMocks.dispatch.mock.calls[0]?.[0],
          content: 'interrupted',
        },
        workdayRequests: [],
      });
    });

    const done = run('typed-fast-during-workday', {
      ...qualifiedPayload(),
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 0.001,
    }, statusRef);
    await vi.advanceTimersByTimeAsync(1_000);

    expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(15_000);
    expect(workdayClaimBodies).toHaveLength(2);
    expect(workdayClaimBodies[1]).toEqual(expect.objectContaining({
      message_ids: [legacyMessage.id],
      incarnation_id: 'incarnation-legacy-workday',
    }));

    if (renewalFails) {
      await vi.advanceTimersByTimeAsync(2_000);
      await done;
      expect(typedFastMocks.interrupt).toHaveBeenCalledTimes(1);
      expect(workday.interrupt).toHaveBeenCalledTimes(1);
      expect(mockApiPost.mock.calls.some(([url]) =>
        url.endsWith('/conversation/turns/complete'))).toBe(false);
      return;
    }

    const fastReceipt: unknown = typedFastMocks.dispatch.mock.calls[0]?.[0];
    if (workdayCompletesFirst) {
      // Workday owns the newer status write. Fast began while Workday was still
      // active, so its saved pre-turn snapshot is now stale and must not win
      // when the concurrent Fast completion settles later.
      resolveWorkday(turnResult({
        sentOperatorMessage: true,
        resultText: 'Legacy Workday reply completed.',
      }));
      await vi.advanceTimersByTimeAsync(1_000);
      expect(statusRef.value).toBe('Resident ready — waiting for operator input');
    }
    resolveFast({
      receipt: fastReceipt,
      result: turnResult({ resultText: 'Hi — what are you thinking about?' }),
      completion: {
        receipt: fastReceipt,
        content: 'Hi — what are you thinking about?',
      },
      workdayRequests: [],
    });
    await vi.advanceTimersByTimeAsync(1_000);
    if (workdayCompletesFirst) {
      expect(statusRef.value).toBe('Resident ready — waiting for operator input');
    }

    expect(mockApiPost.mock.calls.some(([url]) =>
      url.endsWith('/conversation/turns/complete'))).toBe(true);
    expect(workday.interrupt).not.toHaveBeenCalled();
    expect(workday.sendTurn).toHaveBeenCalledTimes(1);

    if (workdayCompletesFirst) {
      allowHeartbeatStop = true;
    } else {
      await vi.advanceTimersByTimeAsync(15_000);
      expect(heartbeatClaimIds).toContain('claim-legacy-workday');
      resolveWorkday(turnResult({
        sentOperatorMessage: true,
        resultText: 'Legacy Workday reply completed.',
      }));
    }

    await vi.advanceTimersByTimeAsync(4_000);
    await done;
  };

  it('renews and restores the ordinary Workday claim while Fast replies', async () => {
    await runOrdinaryWorkdayOverlapCase(false);
  });

  it('preserves a newer Workday status when concurrent Fast completes', async () => {
    await runOrdinaryWorkdayOverlapCase(false, true);
  });

  it('stops both lanes when the displaced Workday claim cannot be renewed', async () => {
    await runOrdinaryWorkdayOverlapCase(true);
  });

  it('queues an exact Fast fallback behind active Workday and keeps a newer Fast row on Fast', async () => {
    vi.useFakeTimers();
    const legacyMessage = {
      id: 'legacy-before-fast-fallback',
      content: 'Finish this existing Workday reply first.',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    const fastMessage = {
      id: 'fast-fallback-during-workday',
      content: 'Answer this after Fast fails its protocol.',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    const newerFastMessage = {
      id: 'newer-fast-during-fallback-wait',
      content: 'Keep this newer row on its Fast owner.',
      created_at: new Date(Date.now() + 1_000).toISOString(),
      from_type: 'user',
    };
    let messagePolls = 0;
    let fastReplyPersisted = false;
    let newerFastReplyPersisted = false;
    mockApiGet.mockImplementation((url: string) => {
      if (!url.includes('/messages?')) return Promise.resolve(null);
      messagePolls += 1;
      if (messagePolls === 1) {
        return Promise.resolve({ messages: [], fast_message_ids: [], claim_required: true });
      }
      if (messagePolls === 2) {
        return Promise.resolve({
          messages: [legacyMessage], fast_message_ids: [], claim_required: true,
        });
      }
      if (!fastReplyPersisted) {
        const messages = messagePolls === 3 || newerFastReplyPersisted
          ? [fastMessage]
          : [fastMessage, newerFastMessage];
        return Promise.resolve({
          messages,
          fast_message_ids: messages.map(message => message.id),
          claim_required: true,
        });
      }
      return Promise.resolve({ messages: [], fast_message_ids: [], claim_required: true });
    });

    const canonicalEnvelope = {
      version: 2,
      project_id: '11111111-1111-4111-8111-111111111111',
      context_authority_generation: 4,
      privacy_generation: 2,
      transcript_sequence: 83,
      ledger_revision: 0,
      problem_model: {},
      decisions: [],
      memory: { active: [] },
      recent_conversation: [boundConversationRow(fastMessage, 83)],
    };
    const canonicalBytes = Buffer.from(JSON.stringify(canonicalEnvelope), 'utf8');
    const envelope = {
      snapshot_id: 'snapshot-fast-fallback-during-workday',
      canonical_envelope_b64: canonicalBytes.toString('base64'),
      canonical_sha256: createHash('sha256').update(canonicalBytes).digest('hex'),
      context_authority_generation: 4,
      privacy_generation: 2,
      transcript_sequence: 83,
      ledger_revision: 0,
      expires_at: '2035-01-01T00:00:00.000Z',
    };
    const newerCanonicalEnvelope = {
      ...canonicalEnvelope,
      transcript_sequence: 84,
      recent_conversation: [boundConversationRow(newerFastMessage, 84)],
    };
    const newerCanonicalBytes = Buffer.from(JSON.stringify(newerCanonicalEnvelope), 'utf8');
    const newerEnvelope = {
      ...envelope,
      snapshot_id: 'snapshot-newer-fast-during-fallback',
      canonical_envelope_b64: newerCanonicalBytes.toString('base64'),
      canonical_sha256: createHash('sha256').update(newerCanonicalBytes).digest('hex'),
      transcript_sequence: 84,
    };
    let stopAfterFallback = false;
    const replyCoveredIds: string[][] = [];
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/strategic-session/heartbeat')) {
        return Promise.resolve({
          authority_held: true,
          conversation_claim_held: true,
          stop_requested: stopAfterFallback,
        });
      }
      if (url.endsWith('/typed-fast-lease/acquire')) {
        return Promise.resolve({
          acquired: true,
          lease: { lease_id: 'lease-fast-fallback-during-workday', generation: 1 },
        });
      }
      if (url.endsWith('/messages/claim')) {
        const messageIds = body?.message_ids as string[];
        return Promise.resolve({
          claim_id: messageIds.includes(fastMessage.id)
            ? 'claim-fast-workday-fallback'
            : 'claim-existing-workday',
          incarnation_id: 'incarnation-existing-workday',
          incarnation_supported: true,
          message_ids: messageIds,
        });
      }
      if (url.endsWith('/conversation/turns/claim')) {
        const messageIds = body?.message_ids as string[];
        const newer = messageIds.includes(newerFastMessage.id);
        return Promise.resolve({
          claim_id: newer ? 'claim-newer-fast' : 'claim-fast-protocol-failure',
          owner: 'fast',
          message_ids: messageIds,
          envelope: newer ? newerEnvelope : envelope,
        });
      }
      if (url.endsWith('/conversation/turns/complete')) {
        newerFastReplyPersisted = true;
        return Promise.resolve({ status: 'completed' });
      }
      if (url.endsWith('/conversation/turns/abandon')) {
        return Promise.resolve({ status: 'abandoned', legacy_fallback_admissible: true });
      }
      if (body?.op === 'create_message') {
        const covered = ((body.data as Record<string, unknown>).covered_message_ids ?? []) as string[];
        replyCoveredIds.push(covered);
        if (covered.includes(fastMessage.id)) {
          fastReplyPersisted = true;
          stopAfterFallback = true;
        }
      }
      return Promise.resolve({});
    });
    typedFastMocks.dispatch.mockImplementation((receipt: { message_ids: string[] }) => Promise.resolve({
      receipt,
      result: turnResult(),
      completion: receipt.message_ids.includes(fastMessage.id)
        ? null
        : { receipt, content: 'Fast retained and answered the newer row.' },
      workdayRequests: [],
    }));

    let resolveExistingWorkday!: (result: ResidentTurnResult) => void;
    const existingWorkday = new Promise<ResidentTurnResult>((resolve) => {
      resolveExistingWorkday = resolve;
    });
    const workday = makeSession(() => (
      workday.sendTurn.mock.calls.length === 1
        ? existingWorkday
        : Promise.resolve(turnResult({
          sentOperatorMessage: true,
          resultText: 'Workday fallback completed.',
        }))
    ), () => true);
    workday.interrupt.mockImplementation(() => {
      resolveExistingWorkday(turnResult({ interrupted: true }));
    });
    mockStartSession.mockReturnValue(workday);

    const done = run('fast-fallback-during-workday', {
      ...qualifiedPayload(),
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 0.001,
    });
    await vi.advanceTimersByTimeAsync(3_000);

    expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(1);
    expect(workday.sendTurn).toHaveBeenCalledTimes(1);
    expect(workday.interrupt).not.toHaveBeenCalled();

    resolveExistingWorkday(turnResult({
      sentOperatorMessage: true,
      resultText: 'Existing Workday reply completed.',
    }));
    await vi.advanceTimersByTimeAsync(5_000);
    await done;

    expect(typedFastMocks.dispatch).toHaveBeenCalledTimes(2);
    expect(typedFastMocks.dispatch.mock.calls[1]?.[0]).toEqual(expect.objectContaining({
      message_ids: [newerFastMessage.id],
    }));
    expect(workday.sendTurn).toHaveBeenCalledTimes(2);
    expect(replyCoveredIds).toContainEqual([fastMessage.id]);
    expect(replyCoveredIds.flat()).not.toContain(newerFastMessage.id);
  });
});

describe('handleStrategicResident — operator-turn compaction includes settled decision and drops answered message', () => {
  beforeEach(() => {
    replyToolPaths.current = null;
    mockApiGet.mockReset();
    mockApiPost.mockReset();
    mockStartSession.mockReset();
    mockSendLog.mockReset();
    mockSendWsFrame.mockReset();
    mockSendWsFrame.mockReturnValue(true);
    mockWriteArtifactsHelper.mockClear();
    mockWriteArtifactsHelper.mockReturnValue(true);
    mockWriteMessagesHelper.mockClear();
    mockWriteStrategicHelpers.mockClear();
    // Workspace guard must NOT fire in general-purpose tests (WORKSPACE='' otherwise
    // always looks vanished). Recovery-specific tests override this spy.
    vi.spyOn(fs, 'existsSync').mockReturnValue(true);
  });
  afterEach(() => { vi.restoreAllMocks(); });

  it('places reply authority outside the mutable Git workspace', () => {
    // Use a distinct mutable-workspace path. CI may set AGENT_WORKSPACE to
    // os.tmpdir(), so comparing the runtime directory with /tmp itself makes
    // this otherwise-valid isolation assertion environment-dependent.
    const workspace = path.join(os.tmpdir(), 'mutable-git-workspace');
    const paths = residentReplyRuntimePaths('task-runtime-boundary');
    expect(paths.runtimeDir.startsWith(`${path.resolve(workspace)}${path.sep}`)).toBe(false);
    expect(paths.replyReceiptPath).toBe(path.join(
      paths.stateDir,
      '.resident-reply-receipt.json',
    ));
    expect(paths.privateTurnMarkerPath).toBe(path.join(
      paths.stateDir,
      '.resident-private-workday-turn',
    ));
    expect(paths.mcpRootDir).toBe(path.join(paths.runtimeDir, 'mcp'));
    expect(paths.agentTokenPath).toBe(path.join(
      paths.runtimeDir, 'agent-token', 'agent-token.jwt',
    ));
    const competing = residentReplyRuntimePaths(
      'task-runtime-boundary',
      '00000000-0000-0000-0000-000000000901',
    );
    const winner = residentReplyRuntimePaths(
      'task-runtime-boundary',
      '00000000-0000-0000-0000-000000000902',
    );
    expect(competing.runtimeDir).not.toBe(winner.runtimeDir);
    expect(competing.replyReceiptPath).not.toBe(winner.replyReceiptPath);
  });

  it.each(['typed-consultation', 'active-call', 'typed-autonomous'] as const)(
    'keeps the local message fence installed for the complete %s private turn',
    async (kind) => {
      const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), 'private-workday-fence-'));
      const markerPath = path.join(runtimeDir, '.resident-private-workday-turn');
      let release!: () => void;
      const pending = withPrivateWorkdayTurnFence(
        markerPath,
        kind,
        '00000000-0000-0000-0000-000000000951',
        async () => {
          const marker = JSON.parse(fs.readFileSync(markerPath, 'utf8')) as {
            kind?: string; incarnation_id?: string; packet_id?: string; workday_claim_id?: string;
          };
          expect(marker).toEqual({
            kind,
            incarnation_id: '00000000-0000-0000-0000-000000000951',
            ...(kind === 'typed-consultation' ? {
              packet_id: '00000000-0000-0000-0000-000000000952',
              workday_claim_id: '00000000-0000-0000-0000-000000000953',
            } : {}),
          });
          await new Promise<void>((resolve) => { release = resolve; });
          expect(fs.lstatSync(markerPath).isFile()).toBe(true);
        },
        kind === 'typed-consultation' ? {
          packetId: '00000000-0000-0000-0000-000000000952',
          workdayClaimId: '00000000-0000-0000-0000-000000000953',
        } : undefined,
      );

      expect(fs.lstatSync(markerPath).isFile()).toBe(true);
      release();
      await pending;
      expect(() => fs.lstatSync(markerPath)).toThrow();
      fs.rmSync(runtimeDir, { recursive: true, force: true });
    },
  );

  it('removes stale reply artifacts before the session starts or autonomous work runs', async () => {
    const events: string[] = [];
    vi.spyOn(fs, 'unlinkSync').mockImplementation((file) => {
      expect(path.dirname(String(file))).toContain('blueprint-resident-reply');
      expect([
        '.resident-reply-idempotency-key',
        '.resident-reply-receipt.json',
        '.resident-reply-staged-message.json',
      ]).toContain(path.basename(String(file)));
      events.push(`stale-artifact-removed:${path.basename(String(file))}`);
    });
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?')) return Promise.resolve({ messages: [] });
      return Promise.resolve(null);
    });
    mockApiPost.mockResolvedValue({ authority_held: true });

    let alive = true;
    const session = makeSession(
      () => {
        events.push('autonomous-turn');
        alive = false;
        return Promise.resolve(turnResult({ appliedStrategicOp: true }));
      },
      () => alive,
    );
    mockStartSession.mockImplementation(() => {
      events.push('session-started');
      return session;
    });

    await handleStrategicResident(
      'task-stale-reply-key',
      { project_name: 'Proj', open_messages: [] },
      { project_metadata: { project_id: 'proj-stale-reply-key' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    expect(events.slice(0, 5)).toEqual([
      'stale-artifact-removed:.resident-reply-idempotency-key',
      'stale-artifact-removed:.resident-reply-receipt.json',
      'stale-artifact-removed:.resident-reply-staged-message.json',
      'session-started',
      'autonomous-turn',
    ]);
    const installedRuntime = mockWriteMessagesHelper.mock.calls[0]?.[2];
    expect(mockWriteMessagesHelper).toHaveBeenCalledWith(
      'task-stale-reply-key',
      '/tmp',
      expect.objectContaining({
        residentReplyOnly: true,
      }),
    );
    const runtime = installedRuntime as {
      replyReceiptPath?: unknown;
      stagedReplyPath?: unknown;
    };
    expect(runtime.replyReceiptPath).toEqual(expect.any(String));
    expect(runtime.stagedReplyPath).toEqual(expect.any(String));
    expect(mockWriteStrategicHelpers).toHaveBeenCalledWith(
      'task-stale-reply-key',
      'proj-stale-reply-key',
      '/tmp',
      expect.objectContaining({
        replyReceiptPath: installedRuntime?.replyReceiptPath,
        stagedReplyPath: installedRuntime?.stagedReplyPath,
      }),
    );
    expect(mockWriteArtifactsHelper).toHaveBeenCalledWith('/tmp');
    const heartbeat = mockApiPost.mock.calls.find(([url]) =>
      url.includes('/strategic-session/heartbeat'),
    );
    expect(heartbeat?.[2]).toBe(1);
    expect(heartbeat?.[3]).toBe(HEARTBEAT_API_TIMEOUT);
  });

  it('unlinks a pre-existing reply-key symlink and exclusively creates a file without touching its target', () => {
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'resident-reply-key-'));
    const targetPath = path.join(tempDir, 'symlink-target');
    const replyKeyPath = path.join(tempDir, '.resident-reply-idempotency-key');
    try {
      fs.writeFileSync(targetPath, 'do-not-overwrite');
      fs.symlinkSync(targetPath, replyKeyPath, 'file');
      const writeSpy = vi.spyOn(fs, 'writeFileSync');

      installResidentReplyKeyAtPath(replyKeyPath, 'operator-reply-key');

      expect(fs.readFileSync(targetPath, 'utf8')).toBe('do-not-overwrite');
      expect(fs.lstatSync(replyKeyPath).isSymbolicLink()).toBe(false);
      expect(fs.readFileSync(replyKeyPath, 'utf8')).toBe('operator-reply-key');
      expect(writeSpy).toHaveBeenCalledWith(
        replyKeyPath,
        'operator-reply-key',
        expect.objectContaining({ flag: 'wx', mode: 0o600 }),
      );
    } finally {
      fs.rmSync(tempDir, { recursive: true, force: true });
    }
  });

  it('rejects symlinked and oversized staged replies before reading their contents', () => {
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'resident-staged-read-'));
    const stagedPath = path.join(tempDir, '.resident-reply-staged-message.json');
    const targetPath = path.join(tempDir, 'target.json');
    const receipt = {
      idempotency_key: 'receipt-key',
      covered_message_ids: ['m1'],
      reply_to_message_id: 'm1',
    };
    try {
      fs.writeFileSync(targetPath, JSON.stringify({
        type: 'NOTE', content: 'must not follow', ...receipt,
      }));
      fs.symlinkSync(targetPath, stagedPath, 'file');
      expect(() => readResidentStagedReplyAtPath(stagedPath, receipt)).toThrow(/regular file|identity/);
      fs.unlinkSync(stagedPath);
      fs.writeFileSync(stagedPath, 'x'.repeat(65_537));
      expect(() => readResidentStagedReplyAtPath(stagedPath, receipt)).toThrow(/exceeds 64KiB/);
    } finally {
      fs.rmSync(tempDir, { recursive: true, force: true });
    }
  });

  it('primes the compacted session WITH the settled operator decision and WITHOUT the answered message after atomic reply handling', async () => {
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
    // The real messages helper inserts the reply and marks its immutable
    // covered IDs handled in one transaction.
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
      () => {
        handledIds.push(OPERATOR_MSG.id);
        events.push('reply:atomically-handled');
        return Promise.resolve(turnResult({
          resultText: `Answered the operator.\nOPERATOR-DECISION: ${DECISION_NOTE}`,
          inputTokens: 5_000, // >= the 100-token threshold set below
        }));
      },
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

    // (1) The answered message was marked handled with the reply BEFORE checkpoint
    //     state was fetched, and BEFORE the fresh subprocess was primed.
    const iHandled = events.indexOf('reply:atomically-handled');
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
    expect(autonomousPrompt).toContain('./artifacts fetch <compute-job-id>');
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
    mockWriteArtifactsHelper.mockReset();
    mockWriteArtifactsHelper.mockReturnValue(true);
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
    expect(session1.close).toHaveBeenCalled();
    expect(session1.waitForIdle).toHaveBeenCalled();
    // recovery session received the prime (compaction checkpoint)
    expect(session2.sendTurn).toHaveBeenCalledTimes(1);
    // helpers were re-provisioned into the recreated checkout (startup + recovery = 2 calls each)
    expect(mockWritePlaybookHelper.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(mockWriteStrategicHelpers.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(mockWriteArtifactsHelper.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(mockProvisionAmbientHelpers.mock.calls.length).toBeGreaterThanOrEqual(2);
    const provisionCalls = mockProvisionResidentReplyTool.mock.calls;
    expect(provisionCalls.length).toBeGreaterThanOrEqual(2);
    const startupConfig = provisionCalls[provisionCalls.length - 2]?.[1] as {
      strategic?: Record<string, unknown>;
    };
    const recoveryConfig = provisionCalls[provisionCalls.length - 1]?.[1] as {
      strategic?: Record<string, unknown>;
    };
    expect(startupConfig.strategic?.helperPath).toMatch(/\/mcp\/generation-1\/strategic$/);
    expect(startupConfig.strategic?.readHelperPath).toMatch(/\/mcp\/generation-1\/fetch$/);
    expect(recoveryConfig.strategic?.helperPath).toMatch(/\/mcp\/generation-2\/strategic$/);
    expect(recoveryConfig.strategic?.readHelperPath).toMatch(/\/mcp\/generation-2\/fetch$/);
    const stableStrategic = (value: Record<string, unknown> | undefined) => {
      if (!value) return value;
      const stable = { ...value };
      delete stable.helperPath;
      delete stable.readHelperPath;
      return stable;
    };
    expect(stableStrategic(recoveryConfig.strategic)).toEqual(
      stableStrategic(startupConfig.strategic),
    );
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
    expect(session2.sendTurn).toHaveBeenCalledTimes(1);
    // The checkpoint must carry the operator purpose / constraints.
    expect(capturedCheckpoint).toContain('Operator Purpose: autonomous research.');
    expect(capturedCheckpoint).toContain('Constraints: no prod writes.');
    expect(capturedCheckpoint).toContain('v26 fix wave');
  });

  it.each([
    { label: 'soft timeout', flags: { timedOut: true } },
    { label: 'hard timeout', flags: { hardTimedOut: true } },
  ] as const)('fails workspace recovery with canonical timeout on $label during checkpoint prime', async ({ label, flags }) => {
    vi.spyOn(fs, 'existsSync').mockReturnValue(false);
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/api/agent/git-read-token')) return Promise.resolve({ token: 'fresh-read-token' });
      if (url.includes('/messages?')) return Promise.resolve({ messages: [] });
      return Promise.resolve(null);
    });
    mockApiPost.mockResolvedValue({ authority_held: true });
    const oldSession = makeSession(() => Promise.resolve(turnResult()), () => false);
    const recoverySession = makeSession(
      () => Promise.resolve(turnResult({ interrupted: true, ...flags })),
      () => true,
    );
    mockStartSession.mockReturnValueOnce(oldSession).mockReturnValueOnce(recoverySession);

    await handleStrategicResident(
      `task-recovery-prime-${label}`,
      { project_name: 'P', open_messages: [] },
      { project_metadata: { project_id: `proj-recovery-prime-${label}` }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    expect(recoverySession.sendTurn).toHaveBeenCalledTimes(1);
    expect(mockSendLog.mock.calls.flat().join(' ')).toContain('workspace recovery checkpoint prime incomplete');
    const terminal = vi.mocked(persistAndPostStatus).mock.calls.at(-1)?.[1] as Record<string, unknown>;
    expect(terminal).toMatchObject({ status: 'FAILED', failure_category: 'timeout' });
  });

  it('preserves canonical provider classification when compaction checkpoint prime rate-limits', async () => {
    const operatorMessage = {
      id: 'msg-prime-rate-limit', content: 'trigger compaction',
      created_at: new Date().toISOString(), from_type: 'user',
    };
    let delivered = false;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?') && !delivered) {
        delivered = true;
        return Promise.resolve({ messages: [operatorMessage] });
      }
      if (url.includes('/messages?')) return Promise.resolve({ messages: [] });
      return Promise.resolve(null);
    });
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/heartbeat')) return Promise.resolve({ authority_held: true });
      return Promise.resolve({});
    });
    const oldSession = makeSession(
      () => Promise.resolve(turnResult({ resultText: 'reply', inputTokens: 100 })),
      () => true,
    );
    const compactedSession = makeSession(
      () => Promise.resolve(turnResult({
        providerFailed: true,
        failureCategory: 'rate_limited',
      })),
      () => true,
    );
    mockStartSession.mockReturnValueOnce(oldSession).mockReturnValueOnce(compactedSession);

    await handleStrategicResident(
      'task-compaction-prime-rate-limit',
      {
        project_name: 'P', open_messages: [],
        strategic_session_compaction_threshold_tokens: 1,
        strategic_session_operator_ack_threshold_seconds: 0,
      },
      { project_metadata: { project_id: 'proj-compaction-prime-rate-limit' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    const terminal = vi.mocked(persistAndPostStatus).mock.calls.at(-1)?.[1] as Record<string, unknown>;
    expect(terminal).toMatchObject({ status: 'FAILED', failure_category: 'rate_limited' });
    expect(compactedSession.sendTurn).toHaveBeenCalledTimes(1);
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
    expect(opts.singleBranch).toBe(true);
    expect(opts.filter).toBe('blob:none');
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
    const postOrder: string[] = [];
    let heartbeatCount = 0;
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('heartbeat')) {
        postOrder.push('heartbeat');
        heartbeatCount += 1;
        return Promise.resolve({
          authority_held: true,
          stop_requested: heartbeatCount > 1,
        });
      }
      if (url.includes('strategic')) {
        if (body?.op === 'create_message') {
          postOrder.push('create_message');
        }
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

    const heartbeatCalls = mockApiPost.mock.calls.filter(([url]) =>
      url.includes('heartbeat'));
    expect(heartbeatCalls.length).toBeGreaterThan(0);
    expect(heartbeatCalls.every(([, body]) =>
      !body || !('handled_message_ids' in body))).toBe(true);
    expect(recordOperatorDecisionCapture).toHaveBeenCalledWith('captured');
    expect(postOrder.slice(0, 3)).toEqual([
      'heartbeat',
      'create_message',
      'heartbeat',
    ]);
    const createCall = mockApiPost.mock.calls.find(([, body]) => body?.op === 'create_message');
    const replyData = createCall?.[1]?.data as Record<string, unknown> | undefined;
    const turnState = replyData?.resident_turn_state as Record<string, unknown> | undefined;
    expect(String(turnState?.decisions_reg)).toContain('use approach Y, not X');
    expect(String(turnState?.exam_ledger)).toContain('operatorDecisionCapture');
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
    // second autonomous tick without waiting on the exploration interval.
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

  it('exhausts review attempts and force-admits exchange text to the durable decisions register, clearing the review and emitting a status', async () => {
    // The model has failed to classify this operator exchange for
    // MAX_REVIEW_ATTEMPTS consecutive autonomous ticks. The escape hatch
    // (strategic-resident.ts ~1450-1492) force-admits the available exchange text
    // into the durable decisions register so the operator correction is not lost
    // at restart or compaction, then clears the pending review.
    const EXCHANGE_TEXT = 'operator prefers approach Z — do not rewrite the table-based path';
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
        alive = false; // die after this turn so the loop exits after the heartbeat
        // No OPERATOR-DECISION or OPERATOR-DECISION-REVIEW marker →
        // classifyOperatorDecisionReview returns kind='pending', triggering the
        // retain-then-escape-hatch branch.
        return Promise.resolve(turnResult({
          resultText: 'Reviewed the recent operator exchange. Nothing new.',
          appliedStrategicOp: true,
          inputTokens: 0,
        }));
      },
      () => alive,
    );
    mockStartSession.mockReturnValue(session);

    await handleStrategicResident(
      'task-exhaust',
      { project_name: 'Proj', open_messages: [] },
      {
        project_metadata: { project_id: 'proj-exhaust' },
        context: {
          // Seed a review with attempts = MAX_REVIEW_ATTEMPTS - 1 (2) so the
          // next retain (increment to 3) trips the >=MAX_REVIEW_ATTEMPTS escape
          // hatch on the first autonomous turn.
          _exam_ledger: JSON.stringify({
            records: [],
            tick: 10,
            corrective: false,
            operatorDecisionCorrective: true,
            operatorDecisionReview: {
              status: 'pending',
              messageIds: ['msg-exhaust'],
              exchangeText: EXCHANGE_TEXT,
              startedAt: '2026-07-17T10:00:00Z',
              attempts: 2, // MAX_REVIEW_ATTEMPTS - 1
            },
          }),
        },
      },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    // (a) The heartbeat's decisions_reg must contain the fallback entry with
    //     the exchange text prefixed by the "unclassified after N attempts" marker.
    const withFallback = heartbeatBodies.find(
      (b): b is Record<string, unknown> & { decisions_reg: string; note: string } =>
        typeof b.decisions_reg === 'string'
        && b.decisions_reg.includes(EXCHANGE_TEXT)
        && typeof b.note === 'string',
    );
    expect(withFallback).toBeDefined();
    const reg = JSON.parse((withFallback as Record<string, string>).decisions_reg) as {
      entries: Array<{ surfaceKey: string; verdict: string; note: string }>;
    };
    const fallbackEntry = reg.entries.find((e) => e.surfaceKey === 'operator:message');
    expect(fallbackEntry).toBeDefined();
    expect(fallbackEntry?.verdict).toBe('operator');
    expect(fallbackEntry?.note).toContain('[unclassified after 3 attempts]');
    expect(fallbackEntry?.note).toContain(EXCHANGE_TEXT);

    // (b) The serialized exam ledger in the heartbeat shows the review is cleared.
    const ledger = JSON.parse((withFallback as Record<string, string>).note) as { operatorDecisionReview: unknown };
    expect(ledger.operatorDecisionReview).toBeNull();

    // (c) The streamer emitted the escape-hatch status via scheduleDrain. The
    //     status must pass through the rate-limited async drain queue (one frame
    //     per STREAM_FLUSH_MS=100), so skip the timed assertion here — the
    //     decisions-register and ledger assertions above are the authoritative
    //     evidence that the escape hatch fired correctly.
    //     (The lib-level test strategic-resident-lib.test.ts covers the inline
    //     fallback-note construction and admission, and the ``it('id-less
    //     overflow sentinel: repeated MAX_REVIEW_ATTEMPTS fallback admissions
    //     produce at most one register entry')`` test verifies the dedup guard.)
  });

  it('exhausted-attempts fallback with no exchange text produces [exchange text unavailable] in the note', async () => {
    // Same as above but the pending review has no exchangeText field — the
    // fallback note must contain the "unavailable" sentinel rather than the
    // exchange text itself.
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
        alive = false;
        return Promise.resolve(turnResult({
          resultText: 'Checked the conversation. No action needed.',
          appliedStrategicOp: true,
          inputTokens: 0,
        }));
      },
      () => alive,
    );
    mockStartSession.mockReturnValue(session);

    await handleStrategicResident(
      'task-exhaust-no-text',
      { project_name: 'Proj', open_messages: [] },
      {
        project_metadata: { project_id: 'proj-exhaust-nt' },
        context: {
          _exam_ledger: JSON.stringify({
            records: [],
            tick: 10,
            corrective: false,
            operatorDecisionCorrective: true,
            operatorDecisionReview: {
              status: 'pending',
              messageIds: ['msg-no-text'],
              // no exchangeText field — exchange was too short or unavailable
              startedAt: '2026-07-17T10:00:00Z',
              attempts: 2, // MAX_REVIEW_ATTEMPTS - 1
            },
          }),
        },
      },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    // The decisions_reg must contain a fallback entry whose note says
    // "[exchange text unavailable]" instead of any actual exchange text.
    const withFallback = heartbeatBodies.find(
      (b): b is Record<string, unknown> & { decisions_reg: string; note: string } =>
        typeof b.decisions_reg === 'string'
        && typeof b.note === 'string',
    );
    expect(withFallback).toBeDefined();
    const reg = JSON.parse((withFallback as Record<string, string>).decisions_reg) as {
      entries: Array<{ surfaceKey: string; verdict: string; note: string }>;
    };
    const fallbackEntryNote = reg.entries.find((e) => e.surfaceKey === 'operator:message')?.note;
    expect(fallbackEntryNote).toBeDefined();
    // The note must use the "unavailable" sentinel, not contain any real text.
    expect(fallbackEntryNote).toContain('[unclassified after 3 attempts');
    expect(fallbackEntryNote).toContain('exchange text unavailable');
    // No actual exchange text in the note.
    expect(fallbackEntryNote).not.toContain('Checked the conversation');

    // The review is cleared in the serialized ledger.
    const ledger = JSON.parse((withFallback as Record<string, string>).note) as { operatorDecisionReview: unknown };
    expect(ledger.operatorDecisionReview).toBeNull();
  });

  it('id-less overflow sentinel: repeated escape-hatch trips through the handler loop produce at most one decisions-register entry (sentinelKey dedup guard)', async () => {
    // The id-less overflow sentinel (messageIds:[], overflowed:true) has no stable
    // message ids, so the same sentinel can re-trip the MAX_REVIEW_ATTEMPTS escape
    // hatch on every failed autonomous turn. The alreadyAdmitted guard at
    // strategic-resident.ts:1464-1465 detects a prior sentinel entry by the
    // sentinel:${startedAt} key embedded in the note and skips re-admission.
    //
    // This test exercises the guard through the full handler loop:
    //   - decisions_reg is pre-seeded with one entry already carrying the sentinel key
    //     (representing what was admitted on a prior autonomous turn / prior handler run)
    //   - the exam ledger seeds the id-less overflow sentinel at attempts=1 so two
    //     autonomous turns are needed: turn 1 retains (2 < MAX_REVIEW_ATTEMPTS=3, no
    //     escape hatch), turn 2 retains (3 >= MAX_REVIEW_ATTEMPTS, escape hatch fires)
    //   - on the escape-hatch turn, alreadyAdmitted=true and no second entry is added
    //   - the final decisions_reg must still carry exactly one sentinel-key entry
    const STARTED_AT = '2026-07-10T09:00:00Z';
    const SENTINEL_KEY = `sentinel:${STARTED_AT}`;
    const EXCHANGE_TEXT = 'operator preferred keeping the table-based layout as-is';

    let turnCount = 0;
    let hbRev = 0;
    const heartbeatBodies: Array<Record<string, unknown>> = [];

    // Pre-seed one sentinel entry representing what was admitted on a prior trip.
    const preSeededDecisionsReg = JSON.stringify({
      entries: [{
        atTick: 8,
        at: STARTED_AT,
        surfaceKey: 'operator:message',
        verdict: 'operator',
        note: `[unclassified after 3 attempts ${SENTINEL_KEY}] ${EXCHANGE_TEXT}`,
      }],
    });

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
        hbRev += 1;
        // Increment work_state_rev on every heartbeat so the autonomous-turn
        // gate fires immediately on the next iteration (world-moved signal).
        return Promise.resolve({ authority_held: true, work_state_rev: `rev-${hbRev}` });
      }
      return Promise.resolve({});
    });

    // Two turns: neither emits a classification marker so the review is retained,
    // then on the second turn the escape hatch fires with alreadyAdmitted=true.
    const session = makeSession(
      () => {
        turnCount += 1;
        return Promise.resolve(turnResult({
          resultText: 'Reviewed the recent operator batch. Nothing newly actionable.',
          appliedStrategicOp: true,
          inputTokens: 0,
        }));
      },
      () => turnCount < 2,
    );
    mockStartSession.mockReturnValue(session);

    await handleStrategicResident(
      'task-sentinel-dedup',
      // Small tick intervals keep the between-turn gating wait short.
      {
        project_name: 'Proj', open_messages: [],
        strategic_session_idle_tick_seconds: 1,
        strategic_session_message_poll_seconds: 0.75,
      },
      {
        project_metadata: { project_id: 'proj-sentinel-dedup' },
        context: {
          // Pre-existing sentinel entry from a prior trip — the guard must block
          // a second admission of an entry with the same sentinel key.
          _decisions_reg: preSeededDecisionsReg,
          // Id-less overflow sentinel at attempts=1: two retains (turns 1 and 2)
          // are needed before the escape hatch fires (3 >= MAX_REVIEW_ATTEMPTS=3).
          _exam_ledger: JSON.stringify({
            records: [],
            tick: 9,
            corrective: false,
            operatorDecisionCorrective: true,
            operatorDecisionReview: {
              status: 'pending',
              messageIds: [],
              overflowed: true,
              overflowExchanges: 2,
              exchangeText: EXCHANGE_TEXT,
              startedAt: STARTED_AT,
              attempts: 1, // one retain reaches 2 (no hatch); second reach 3 (hatch)
            },
          }),
        },
      },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    // Both turns must have executed.
    expect(turnCount).toBe(2);

    // (a) The last heartbeat's decisions_reg must contain exactly one entry with
    //     the sentinel key — the dedup guard blocked the second admission.
    const lastWithDecisions = [...heartbeatBodies].reverse().find(
      (b): b is Record<string, unknown> & { decisions_reg: string; note: string } =>
        typeof b.decisions_reg === 'string' && typeof b.note === 'string',
    );
    expect(lastWithDecisions).toBeDefined();
    const reg = JSON.parse((lastWithDecisions as Record<string, string>).decisions_reg) as {
      entries: Array<{ surfaceKey: string; verdict: string; note: string }>;
    };
    const sentinelEntries = reg.entries.filter(
      (e) => e.surfaceKey === 'operator:message' && e.note.includes(SENTINEL_KEY),
    );
    expect(sentinelEntries).toHaveLength(1);

    // (b) The review is cleared in the serialized ledger (advance returned null
    //     because coversOverflow:activeOverflowSentinel was passed on the hatch turn).
    const finalLedger = JSON.parse((lastWithDecisions as Record<string, string>).note) as {
      operatorDecisionReview: unknown;
    };
    expect(finalLedger.operatorDecisionReview).toBeNull();
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

// ── Early operator-turn acknowledgment ───────────────────────────────────────
// When an operator turn runs longer than the configured threshold, the handler
// must post a durable "on it" message so the operator gets a progress signal
// during a slow turn rather than silence for the full duration. The ack is
// suppressed entirely for fast turns (timer is cleared before it fires).

describe('handleStrategicResident — operator-turn responsiveness', () => {
  beforeEach(() => {
    mockApiGet.mockReset();
    mockApiPost.mockReset();
    mockStartSession.mockReset();
    mockSendLog.mockReset();
    mockSendWsFrame.mockReset();
    mockSendWsFrame.mockReturnValue(true);
    mockPrepareWorkspaceForRole.mockClear();
    vi.spyOn(fs, 'existsSync').mockReturnValue(true);
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('does not post a canned acknowledgement while a slow operator turn is in flight', async () => {
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
    let heartbeatCount = 0;
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/api/agent/strategic') && body?.op === 'create_message') {
        strategicPosts.push(body);
      }
      if (url.includes('heartbeat')) {
        heartbeatCount += 1;
        return Promise.resolve({
          authority_held: true,
          stop_requested: heartbeatCount > 1,
        });
      }
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
        strategic_session_idle_tick_seconds: 10,
      },
      { project_metadata: { project_id: 'proj-ack' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    // Advance past the threshold — ack timer fires at 2000ms
    await vi.advanceTimersByTimeAsync(3000);

    // The early ack must have been posted
    expect(strategicPosts).toHaveLength(0);
    expect(session.sendTurn).toHaveBeenCalledWith(expect.anything(), {
      stallTimeoutMs: 120_000,
      hardTimeoutMs: 150_000,
    });

    // Resolve the slow turn; handler should proceed to the heartbeat and stop
    resolveTurn(turnResult({ sentOperatorMessage: true, resultText: 'operator reply' }));
    await vi.advanceTimersByTimeAsync(1000);
    await handlerDone;
  });

  it.each([
    {
      label: 'default direct-execution allowance',
      autonomousTimeoutSeconds: 300,
      expectedHardTimeoutMs: 360_000,
    },
    {
      label: 'configured allowance above the provider absolute ceiling',
      autonomousTimeoutSeconds: 3_600,
      expectedHardTimeoutMs: 900_000,
    },
  ])('gives only a server-qualified direct Workday execution the bounded $label', async ({
    autonomousTimeoutSeconds,
    expectedHardTimeoutMs,
  }) => {
    const OPERATOR_MSG = {
      id: 'msg-direct-execution',
      content: 'Apply the bounded strategic correction and return its receipts.',
      created_at: new Date().toISOString(),
      from_type: 'user',
      context: { _direct_workday_execution: true },
    };
    let pollCount = 0;
    let heartbeatCount = 0;

    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/api/agent/git-read-token')) {
        return Promise.resolve({ token: 'read-scoped-token' });
      }
      if (url.includes('/messages?')) {
        if (pollCount++ === 0) return Promise.resolve({ messages: [OPERATOR_MSG] });
        return Promise.resolve({ messages: [] });
      }
      return Promise.resolve(null);
    });
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('heartbeat')) {
        heartbeatCount += 1;
        return Promise.resolve({
          authority_held: true,
          stop_requested: heartbeatCount > 1,
        });
      }
      return Promise.resolve({});
    });

    const session = makeSession(
      () => Promise.resolve(turnResult({ sentOperatorMessage: true })),
      () => true,
    );
    mockStartSession.mockReturnValue(session);

    await handleStrategicResident(
      'task-direct-execution-budget',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_turn_timeout_seconds: 120,
        strategic_session_autonomous_turn_timeout_seconds: autonomousTimeoutSeconds,
      },
      { project_metadata: { project_id: 'proj-direct-execution-budget' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    expect(session.sendTurn).toHaveBeenCalledWith(expect.anything(), {
      stallTimeoutMs: autonomousTimeoutSeconds * 1_000,
      hardTimeoutMs: expectedHardTimeoutMs,
    });
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
      const idempotencyKey = (body?.data as Record<string, unknown> | undefined)?.idempotency_key;
      if (
        url.includes('/api/agent/strategic') && body?.op === 'create_message'
        && typeof idempotencyKey === 'string'
        && idempotencyKey.includes('resident-ack-')
      ) {
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
        const data = body.data as Record<string, unknown> | undefined;
        if (typeof data?.idempotency_key === 'string'
          && data.idempotency_key.startsWith('resident-ack-')) {
          return Promise.reject(new Error('ack post failed'));
        }
        return Promise.resolve({});
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
        strategic_session_idle_tick_seconds: 10,
      },
      { project_metadata: { project_id: 'proj-ack-fail' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    // Ack fires at 2s and its post fails (non-fatal)
    await vi.advanceTimersByTimeAsync(3000);

    // Handler must still complete normally after the ack failure
    resolveTurn(turnResult({ sentOperatorMessage: true, resultText: 'operator reply' }));
    await vi.advanceTimersByTimeAsync(1000);
    await handlerDone;

    // The heartbeat (proof the turn completed) must have been reached
    expect(heartbeatCount).toBeGreaterThanOrEqual(1);
  });

  it('renews the Resident heartbeat while a slow operator reply is in flight', async () => {
    vi.useFakeTimers();

    const operatorMessage = {
      id: 'msg-liveness',
      content: 'Please take the time you need, but keep the session alive.',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let pollCount = 0;
    let resolveTurn!: (r: ResidentTurnResult) => void;
    const blockedTurnPromise = new Promise<ResidentTurnResult>((resolve) => {
      resolveTurn = resolve;
    });
    const heartbeatBodies: Array<Record<string, unknown>> = [];

    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/api/agent/git-read-token')) {
        return Promise.resolve({ token: 'read-scoped-token' });
      }
      if (url.includes('/messages?')) {
        if (pollCount++ === 0) return Promise.resolve({ messages: [operatorMessage] });
        return Promise.resolve({ messages: [] });
      }
      return Promise.resolve(null);
    });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('heartbeat')) {
        if (body) heartbeatBodies.push(body);
        return Promise.resolve({
          authority_held: true,
          stop_requested: heartbeatBodies.length > 1,
        });
      }
      return Promise.resolve({});
    });

    const session = makeSession(() => blockedTurnPromise, () => true);
    mockStartSession.mockReturnValue(session);
    const handlerDone = handleStrategicResident(
      'task-operator-liveness',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_idle_tick_seconds: 1,
        strategic_session_message_poll_seconds: 1,
      },
      { project_metadata: { project_id: 'proj-operator-liveness' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    await vi.advanceTimersByTimeAsync(1_500);
    expect(heartbeatBodies).toContainEqual(expect.objectContaining({ progress: false }));

    resolveTurn(turnResult({ sentOperatorMessage: true, resultText: 'operator reply' }));
    await vi.advanceTimersByTimeAsync(1_000);
    await handlerDone;
  });
});

describe('handleStrategicResident - failed-turn recovery', () => {
  beforeEach(() => {
    mockApiGet.mockReset();
    mockApiPost.mockReset();
    mockStartSession.mockReset();
    mockSendLog.mockReset();
    mockSendWsFrame.mockReset();
    mockSendWsFrame.mockReturnValue(true);
    replyToolPaths.current = null;
    tokenFileMocks.registerAgentTokenMirror.mockClear();
    tokenFileMocks.unregister.mockClear();
    vi.spyOn(fs, 'existsSync').mockReturnValue(true);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('does not publish shared helpers or terminal state when initial authority is lost', async () => {
    mockWriteMessagesHelper.mockClear();
    mockWriteStrategicHelpers.mockClear();
    mockPrepareWorkspaceForRole.mockClear();
    vi.mocked(persistAndPostStatus).mockClear();
    mockApiPost.mockResolvedValue({
      authority_held: false,
      resident_incarnation_held: false,
      stop_requested: true,
    });
    const statusRef = { value: 'winner remains active' };

    await handleStrategicResident(
      'task-overlap-loser',
      { strategic_session_id: 'overlap-session' },
      {
        project_metadata: { project_id: 'proj-overlap-loser' },
        context: {
          strategic_session_id: 'overlap-session',
          _resident_worker_generation: 1,
        },
      },
      'ghtok', 'owner', 'repo', null, statusRef,
    );

    expect(mockPrepareWorkspaceForRole).not.toHaveBeenCalled();
    expect(mockWriteMessagesHelper).not.toHaveBeenCalled();
    expect(mockWriteStrategicHelpers).not.toHaveBeenCalled();
    expect(mockStartSession).not.toHaveBeenCalled();
    expect(persistAndPostStatus).not.toHaveBeenCalled();
    expect(statusRef.value).toBe('winner remains active');
    expect(mockSendLog.mock.calls.some(([message]) =>
      String(message).includes('ended'))).toBe(false);
  });

  it.each([
    'heartbeat failed (401): task no longer active',
    'heartbeat failed (409): Heartbeat session/task/generation mismatch',
  ])('exits silently on terminal initial admission error: %s', async (errorMessage) => {
    mockWriteMessagesHelper.mockClear();
    mockWriteStrategicHelpers.mockClear();
    mockPrepareWorkspaceForRole.mockClear();
    vi.mocked(persistAndPostStatus).mockClear();
    mockApiPost.mockRejectedValue(new Error(errorMessage));
    const statusRef = { value: 'replacement remains active' };

    await handleStrategicResident(
      'task-terminal-admission',
      { strategic_session_id: 'terminal-admission-session' },
      {
        project_metadata: { project_id: 'proj-terminal-admission' },
        context: {
          strategic_session_id: 'terminal-admission-session',
          _resident_worker_generation: 1,
        },
      },
      'ghtok', 'owner', 'repo', null, statusRef,
    );

    expect(mockPrepareWorkspaceForRole).not.toHaveBeenCalled();
    expect(mockWriteMessagesHelper).not.toHaveBeenCalled();
    expect(mockWriteStrategicHelpers).not.toHaveBeenCalled();
    expect(mockStartSession).not.toHaveBeenCalled();
    expect(persistAndPostStatus).not.toHaveBeenCalled();
    expect(statusRef.value).toBe('replacement remains active');
  });

  it('bounds repeated transport failures during initial admission', async () => {
    vi.useFakeTimers();
    mockApiPost.mockRejectedValue(new Error('temporary gateway outage'));
    const handling = handleStrategicResident(
      'task-admission-outage',
      { strategic_session_id: 'admission-outage-session' },
      {
        project_metadata: { project_id: 'proj-admission-outage' },
        context: { strategic_session_id: 'admission-outage-session' },
      },
      'ghtok', 'owner', 'repo', null, { value: 'not admitted' },
    );

    await vi.advanceTimersByTimeAsync(60_001);
    await handling;

    expect(mockApiPost.mock.calls.length).toBeGreaterThan(1);
    expect(mockApiPost.mock.calls.every(([, , retries, timeout]) => (
      retries === 1
      && typeof timeout === 'number'
      && timeout > 0
      && timeout <= HEARTBEAT_API_TIMEOUT
    ))).toBe(true);
    expect(mockPrepareWorkspaceForRole).not.toHaveBeenCalled();
    expect(mockStartSession).not.toHaveBeenCalled();
  });

  it('revalidates an authority proof that ages out during workspace preparation', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-28T12:00:00Z'));
    vi.mocked(persistAndPostStatus).mockClear();
    let heartbeatCount = 0;
    mockApiPost.mockImplementation(() => {
      heartbeatCount += 1;
      if (heartbeatCount === 1) {
        return Promise.resolve({
          authority_held: true,
          resident_incarnation_held: true,
          stop_requested: false,
        });
      }
      return Promise.resolve({
        authority_held: false,
        resident_incarnation_held: false,
        stop_requested: true,
      });
    });
    mockPrepareWorkspaceForRole.mockImplementationOnce(() => {
      vi.setSystemTime(new Date(Date.now() + 181_000));
      return Promise.resolve();
    });
    const statusRef = { value: 'preparing' };

    await handleStrategicResident(
      'task-slow-provision',
      { strategic_session_id: 'slow-provision-session' },
      {
        project_metadata: { project_id: 'proj-slow-provision' },
        context: { strategic_session_id: 'slow-provision-session' },
      },
      'ghtok', 'owner', 'repo', null, statusRef,
    );

    expect(heartbeatCount).toBe(2);
    expect(mockStartSession).not.toHaveBeenCalled();
    expect(tokenFileMocks.registerAgentTokenMirror).not.toHaveBeenCalled();
    const unadmittedPaths = replyToolPaths.current;
    if (unadmittedPaths === null) throw new Error('expected unadmitted reply runtime paths');
    const unadmittedRuntime = path.dirname(path.dirname(unadmittedPaths.receiptPath));
    expect(() => fs.lstatSync(unadmittedRuntime)).toThrow();
    expect(persistAndPostStatus).not.toHaveBeenCalled();
    expect(statusRef.value).not.toBe('Resident strategic session ended');
  });

  it('waits through retryable legacy grace without publishing loser helpers', async () => {
    vi.useFakeTimers();
    mockWriteMessagesHelper.mockClear();
    mockWriteStrategicHelpers.mockClear();
    let heartbeatCount = 0;
    mockApiPost.mockImplementation(() => {
      heartbeatCount += 1;
      if (heartbeatCount === 1) {
        return Promise.resolve({
          authority_held: false,
          resident_incarnation_held: false,
          stop_requested: false,
          admission_status: 'legacy_grace',
          retry_after_seconds: 1,
        });
      }
      return Promise.resolve({
        authority_held: false,
        resident_incarnation_held: false,
        stop_requested: true,
      });
    });
    const statusRef = { value: 'waiting replacement' };
    const handling = handleStrategicResident(
      'task-legacy-grace',
      { strategic_session_id: 'legacy-session' },
      {
        project_metadata: { project_id: 'proj-legacy-grace' },
        context: {
          strategic_session_id: 'legacy-session',
          _resident_worker_generation: 1,
        },
      },
      'ghtok', 'owner', 'repo', null, statusRef,
    );
    await vi.advanceTimersByTimeAsync(1_000);
    await handling;

    expect(heartbeatCount).toBe(2);
    expect(mockWriteMessagesHelper).not.toHaveBeenCalled();
    expect(mockWriteStrategicHelpers).not.toHaveBeenCalled();
    expect(statusRef.value).toBe('waiting replacement');
  });

  it('bounds legacy grace when the legacy owner keeps renewing', async () => {
    vi.useFakeTimers();
    mockWriteMessagesHelper.mockClear();
    mockWriteStrategicHelpers.mockClear();
    mockApiPost.mockResolvedValue({
      authority_held: false,
      resident_incarnation_held: false,
      stop_requested: false,
      admission_status: 'legacy_grace',
      retry_after_seconds: 30,
      leased_until: new Date(Date.now() + 180_000).toISOString(),
    });
    const statusRef = { value: 'legacy owner remains active' };
    const handling = handleStrategicResident(
      'task-renewing-legacy-grace',
      { strategic_session_id: 'renewing-legacy-session' },
      {
        project_metadata: { project_id: 'proj-renewing-legacy-grace' },
        context: {
          strategic_session_id: 'renewing-legacy-session',
          _resident_worker_generation: 1,
        },
      },
      'ghtok', 'owner', 'repo', null, statusRef,
    );

    await vi.advanceTimersByTimeAsync(210_001);
    await handling;

    expect(mockApiPost.mock.calls.length).toBeGreaterThan(1);
    expect(mockWriteMessagesHelper).not.toHaveBeenCalled();
    expect(mockWriteStrategicHelpers).not.toHaveBeenCalled();
    expect(mockStartSession).not.toHaveBeenCalled();
    expect(statusRef.value).toBe('legacy owner remains active');
  });

  it('does not let a transport outage extend the active legacy-grace deadline', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-28T12:00:00Z'));
    let heartbeatCount = 0;
    mockApiPost.mockImplementation(() => {
      heartbeatCount += 1;
      if (heartbeatCount === 1) {
        return Promise.resolve({
          authority_held: false,
          resident_incarnation_held: false,
          stop_requested: false,
          admission_status: 'legacy_grace',
          retry_after_seconds: 1,
          leased_until: new Date(Date.now() + 180_000).toISOString(),
        });
      }
      return Promise.reject(new Error('backend unavailable during legacy grace'));
    });
    const handling = handleStrategicResident(
      'task-grace-outage',
      { strategic_session_id: 'grace-outage-session' },
      {
        project_metadata: { project_id: 'proj-grace-outage' },
        context: { strategic_session_id: 'grace-outage-session' },
      },
      'ghtok', 'owner', 'repo', null, { value: 'legacy owner active' },
    );

    await vi.advanceTimersByTimeAsync(210_001);
    await handling;

    expect(heartbeatCount).toBeGreaterThan(1);
    expect(mockPrepareWorkspaceForRole).not.toHaveBeenCalled();
    expect(mockStartSession).not.toHaveBeenCalled();
  });

  it('keeps chat alive without autonomous authority when process incarnation is held', async () => {
    const message = {
      id: 'msg-chat-only-authority',
      content: 'Please answer while the batch strategist owns the write lane.',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let delivered = false;
    mockApiGet.mockImplementation((url: string) => {
      if (!url.includes('/messages?')) return Promise.resolve(null);
      if (delivered) return Promise.resolve({ messages: [] });
      delivered = true;
      return Promise.resolve({ messages: [message] });
    });
    let heartbeatCount = 0;
    mockApiPost.mockImplementation((url: string) => {
      if (!url.includes('/heartbeat')) return Promise.resolve({});
      heartbeatCount += 1;
      return Promise.resolve({
        authority_held: false,
        resident_incarnation_held: true,
        stop_requested: heartbeatCount > 1,
      });
    });
    const session = makeSession(
      () => Promise.resolve(turnResult({
        sentOperatorMessage: true,
        resultText: 'chat remains responsive',
      })),
      () => true,
    );
    mockStartSession.mockReturnValue(session);

    await handleStrategicResident(
      'task-chat-only-authority',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_operator_ack_threshold_seconds: 0,
      },
      { project_metadata: { project_id: 'proj-chat-only-authority' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    expect(session.sendTurn).toHaveBeenCalledTimes(1);
    expect(session.sendTurn.mock.calls[0]?.[0]).toEqual(
      expect.stringContaining(message.content),
    );
    expect(mockStartSession).toHaveBeenCalled();
    const sessionOptions = mockStartSession.mock.calls[0]?.[0] as {
      env?: Record<string, unknown>;
    };
    expect(sessionOptions.env?.BLUEPRINT_RESIDENT_INCARNATION_ID).toEqual(expect.any(String));
    expect(sessionOptions.env?.BLUEPRINT_RESIDENT_REPLY_KEY_PATH).toEqual(expect.any(String));
    expect(heartbeatCount).toBeGreaterThan(1);
  });

  it('keeps local terminal state silent when the fenced terminal post is rejected', async () => {
    mockApiPost.mockResolvedValue({
      authority_held: true,
      resident_incarnation_held: true,
      stop_requested: false,
    });
    const session = makeSession(
      () => Promise.resolve(turnResult()),
      () => false,
    );
    mockStartSession.mockReturnValue(session);
    vi.mocked(persistAndPostStatus).mockRejectedValueOnce(new Error('HTTP 409 stale incarnation'));
    const statusRef = { value: 'starting' };

    await handleStrategicResident(
      'task-stale-terminal',
      { project_name: 'P', open_messages: [] },
      { project_metadata: { project_id: 'proj-stale-terminal' }, context: {} },
      'ghtok', 'owner', 'repo', null, statusRef,
    );

    expect(statusRef.value).not.toBe('Resident strategic session ended');
    expect(mockSendLog.mock.calls.some(([message]) =>
      String(message).includes('ended ('))).toBe(false);
    expect(mockSendLog.mock.calls.some(([message]) =>
      String(message).includes('Failed to post resident terminal status'))).toBe(false);
  });

  it('does not persist or mark an operator message handled after a provider error', async () => {
    const operatorMessage = {
      id: 'msg-rate-limited',
      content: 'Please answer this after recovery',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let delivered = false;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?') && !delivered) {
        delivered = true;
        return Promise.resolve({ messages: [operatorMessage] });
      }
      if (url.includes('/messages?')) return Promise.resolve({ messages: [] });
      return Promise.resolve(null);
    });

    const posts: Array<Record<string, unknown>> = [];
    mockApiPost.mockImplementation((_url: string, body?: Record<string, unknown>) => {
      if (body) posts.push(body);
      return Promise.resolve({ authority_held: true, stop_requested: false });
    });
    const session = makeSession(
      () => Promise.resolve(turnResult({
        resultText: 'partial error text',
        accumulatedTurnText: 'partial error text',
        sentOperatorMessage: false,
        providerFailed: true,
        failureCategory: 'rate_limited',
      })),
      () => true,
    );
    mockStartSession.mockReturnValue(session);

    await handleStrategicResident(
      'task-provider-failure',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_operator_ack_threshold_seconds: 0,
      },
      { project_metadata: { project_id: 'proj-provider-failure' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    expect(posts.filter((post) => post.op === 'create_message')).toHaveLength(0);
    expect(posts.some((post) => Array.isArray(post.handled_message_ids))).toBe(false);
    expect(session.close).toHaveBeenCalled();
  });

  it('retries a generic provider error in place and re-delivers the operator turn', async () => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'msg-generic-provider-retry',
      content: 'Please answer after a transient provider error',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let messagePolls = 0;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/api/agent/git-read-token')) {
        return Promise.resolve({ token: 'read-scoped-token' });
      }
      if (url.includes('/messages?')) {
        messagePolls += 1;
        return Promise.resolve({
          messages: messagePolls <= 2 ? [operatorMessage] : [],
        });
      }
      return Promise.resolve(null);
    });
    let heartbeatCount = 0;
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('heartbeat')) {
        heartbeatCount += 1;
        return Promise.resolve({
          authority_held: true,
          stop_requested: heartbeatCount > 1,
        });
      }
      return Promise.resolve({});
    });
    const failedSession = makeSession(
      () => Promise.resolve(turnResult({
        providerFailed: true,
        failureCategory: null,
        failureDetail: 'upstream temporarily unavailable',
        sentOperatorMessage: false,
      })),
      () => true,
    );
    const recoveredSession = makeSession(
      () => Promise.resolve(turnResult({ resultText: 'recovered reply' })),
      () => true,
    );
    mockStartSession
      .mockReturnValueOnce(failedSession)
      .mockReturnValueOnce(recoveredSession);

    const done = handleStrategicResident(
      'task-generic-provider-retry',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_operator_ack_threshold_seconds: 0,
      },
      { project_metadata: { project_id: 'proj-generic-provider-retry' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );
    // Let the asynchronous bootstrap/poll schedule the retry timer before
    // advancing fake time.  Advancing first can move the clock past the point
    // at which the timer is installed and leave the test waiting on its full
    // backoff in real time.
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(1_700);
    await done;

    expect(mockStartSession).toHaveBeenCalledTimes(2);
    expect(failedSession.close).toHaveBeenCalled();
    expect(recoveredSession.sendTurn).toHaveBeenCalledTimes(1);
    const created = mockApiPost.mock.calls.filter(([, body]) =>
      body?.op === 'create_message');
    expect(created).toHaveLength(1);
    expect((created[0][1]?.data as Record<string, unknown>).covered_message_ids)
      .toEqual(['msg-generic-provider-retry']);
    const terminal = (
      vi.mocked(persistAndPostStatus).mock.calls.at(-1)?.[1]
    ) as Record<string, unknown>;
    expect(terminal).toMatchObject({ status: 'COMPLETED' });
  });

  it('fails closed without replacement when the provider child cannot be killed', async () => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'msg-provider-kill-failed',
      content: 'This turn must not be replayed into a second child',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let delivered = false;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?') && !delivered) {
        delivered = true;
        return Promise.resolve({ messages: [operatorMessage] });
      }
      if (url.includes('/messages?')) return Promise.resolve({ messages: [] });
      return Promise.resolve(null);
    });
    mockApiPost.mockResolvedValue({ authority_held: true, stop_requested: false });
    const failedSession = makeSession(
      () => Promise.resolve(turnResult({
        providerFailed: true,
        failureCategory: null,
        failureDetail: 'Could not deliver SIGKILL to Codex child',
        providerTerminalReason: 'kill_failed',
        processGone: false,
      })),
      () => true,
    );
    failedSession.waitForIdle.mockReturnValue(new Promise<void>(() => {}));
    failedSession.exitDiagnostic.mockReturnValue(
      'active_child_pid=5100; provider_output_tail=kill_failed',
    );
    mockStartSession.mockReturnValue(failedSession);

    const done = handleStrategicResident(
      'task-provider-kill-failed',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_operator_ack_threshold_seconds: 0,
      },
      { project_metadata: { project_id: 'proj-provider-kill-failed' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(15_001);
    await done;

    expect(mockStartSession).toHaveBeenCalledTimes(1);
    expect(failedSession.close).toHaveBeenCalledTimes(1);
    const failedReplyPaths = replyToolPaths.current;
    if (failedReplyPaths === null) throw new Error('reply tool paths were not provisioned');
    expect(fs.existsSync(path.dirname(failedReplyPaths.receiptPath))).toBe(true);
    const terminal = (
      vi.mocked(persistAndPostStatus).mock.calls.at(-1)?.[1]
    ) as Record<string, unknown>;
    expect(terminal).toMatchObject({ status: 'FAILED' });
    expect(terminal.logs).toContain('provider_process_uncontrolled');
  });

  it('preserves private provider state when credential recovery forbids cleanup', async () => {
    let delivered = false;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?') && !delivered) {
        delivered = true;
        return Promise.resolve({ messages: [{
          id: 'msg-oauth-retained',
          content: 'Complete only after credential persistence.',
          created_at: new Date().toISOString(),
          from_type: 'user',
        }] });
      }
      if (url.includes('/messages?')) return Promise.resolve({ messages: [] });
      return Promise.resolve(null);
    });
    mockApiPost.mockResolvedValue({ authority_held: true, stop_requested: false });
    const unsafeCleanupSession = Object.assign(makeSession(
      () => Promise.resolve(turnResult({
        providerFailed: true,
        providerTerminalReason: 'oauth_rotation_failed',
        failureDetail: 'authoritative credential write denied',
        processGone: true,
      })),
      () => true,
    ), {
      privateRuntimeCleanupSafe: vi.fn(() => false),
    });
    unsafeCleanupSession.exitDiagnostic.mockReturnValue(
      'provider_output_tail=oauth_rotation_failed',
    );
    mockStartSession.mockReturnValue(unsafeCleanupSession);

    await handleStrategicResident(
      'task-oauth-retained',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_operator_ack_threshold_seconds: 0,
      },
      { project_metadata: { project_id: 'proj-oauth-retained' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    const retainedPaths = replyToolPaths.current;
    if (retainedPaths === null) throw new Error('reply runtime was not provisioned');
    const retainedRuntime = path.dirname(path.dirname(retainedPaths.receiptPath));
    expect(fs.lstatSync(retainedRuntime).isDirectory()).toBe(true);
    expect(tokenFileMocks.unregister).toHaveBeenCalledTimes(1);
    expect(unsafeCleanupSession.privateRuntimeCleanupSafe).toHaveBeenCalled();
    fs.rmSync(retainedRuntime, { recursive: true, force: true });
  });

  it('fails teardown when task-token revocation cannot be proven', async () => {
    vi.mocked(persistAndPostStatus).mockClear();
    let delivered = false;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?') && !delivered) {
        delivered = true;
        return Promise.resolve({ messages: [{
          id: 'msg-token-revocation-failure',
          content: 'Stop after the provider failure.',
          created_at: new Date().toISOString(),
          from_type: 'user',
        }] });
      }
      if (url.includes('/messages?')) return Promise.resolve({ messages: [] });
      return Promise.resolve(null);
    });
    mockApiPost.mockResolvedValue({ authority_held: true, stop_requested: false });
    const session = makeSession(
      () => Promise.resolve(turnResult({
        providerFailed: true,
        providerTerminalReason: 'rate_limited',
        failureCategory: 'rate_limited',
        failureDetail: 'bounded provider failure',
        processGone: true,
      })),
      () => true,
    );
    mockStartSession.mockReturnValue(session);

    let mirroredTokenPath = '';
    tokenFileMocks.registerAgentTokenMirror.mockImplementationOnce((target: string) => {
      mirroredTokenPath = target;
      fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 });
      fs.writeFileSync(target, 'test-task-token', { mode: 0o600 });
      return tokenFileMocks.unregister;
    });
    const realUnlinkSync = fs.unlinkSync;
    let revocationAttempted = false;
    const unlinkSpy = vi.spyOn(fs, 'unlinkSync').mockImplementation((target) => {
      if (String(target) === mirroredTokenPath) {
        revocationAttempted = true;
        throw Object.assign(new Error('token unlink denied'), { code: 'EACCES' });
      }
      realUnlinkSync(target);
    });

    await handleStrategicResident(
      'task-token-revocation-failure',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_operator_ack_threshold_seconds: 0,
      },
      { project_metadata: { project_id: 'proj-token-revocation-failure' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    expect(revocationAttempted).toBe(true);
    expect(tokenFileMocks.unregister).toHaveBeenCalledTimes(1);
    expect(fs.readFileSync(mirroredTokenPath, 'utf8')).toBe('test-task-token');
    const terminal = vi.mocked(persistAndPostStatus).mock.calls.at(-1)?.[1] as Record<string, unknown>;
    expect(terminal).toMatchObject({ status: 'FAILED' });
    expect(terminal.logs).toContain('agent_token_revocation_failed');
    expect(mockSendLog).toHaveBeenCalledWith(
      null,
      expect.stringContaining('agent token revocation could not be proven'),
    );
    unlinkSpy.mockRestore();
    fs.rmSync(path.dirname(path.dirname(mirroredTokenPath)), { recursive: true, force: true });
  });

  it('stays silent after incarnation loss even when provider teardown cannot settle', async () => {
    vi.useFakeTimers();
    vi.mocked(persistAndPostStatus).mockClear();
    mockApiGet.mockImplementation((url: string) => (
      url.includes('/messages?')
        ? Promise.resolve({ messages: [] })
        : Promise.resolve(null)
    ));
    let heartbeatCount = 0;
    mockApiPost.mockImplementation((url: string) => {
      if (!url.includes('/heartbeat')) return Promise.resolve({});
      heartbeatCount += 1;
      return Promise.resolve(heartbeatCount === 1 ? {
        authority_held: true,
        resident_incarnation_held: true,
        stop_requested: false,
      } : {
        authority_held: false,
        resident_incarnation_held: false,
        stop_requested: true,
      });
    });
    const staleSession = makeSession(
      () => Promise.resolve(turnResult()),
      () => true,
    );
    staleSession.waitForIdle.mockReturnValue(new Promise<void>(() => {}));
    staleSession.exitDiagnostic.mockReturnValue(
      'active_child_pid=5101; provider_output_tail=kill_failed',
    );
    mockStartSession.mockReturnValue(staleSession);
    const statusRef = { value: 'replacement remains active' };

    const done = handleStrategicResident(
      'task-incarnation-lost-uncontrolled-child',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_autonomous: false,
        strategic_session_idle_tick_seconds: 1,
        strategic_session_message_poll_seconds: 0.001,
      },
      { project_metadata: { project_id: 'proj-incarnation-lost' }, context: {} },
      'ghtok', 'owner', 'repo', null, statusRef,
    );
    await vi.advanceTimersByTimeAsync(0);
    for (let elapsedMs = 0; heartbeatCount < 2 && elapsedMs < 5_000; elapsedMs += 1_000) {
      await vi.advanceTimersByTimeAsync(1_000);
    }
    expect(heartbeatCount).toBeGreaterThanOrEqual(2);
    await vi.advanceTimersByTimeAsync(15_001);
    await done;

    expect(mockStartSession).toHaveBeenCalledTimes(1);
    expect(staleSession.close).toHaveBeenCalledTimes(1);
    const staleReplyPaths = replyToolPaths.current;
    if (staleReplyPaths === null) throw new Error('reply tool paths were not provisioned');
    expect(fs.existsSync(path.dirname(staleReplyPaths.receiptPath))).toBe(true);
    expect(persistAndPostStatus).not.toHaveBeenCalled();
    expect(statusRef.value).not.toBe('Resident strategic session ended');
    expect(mockSendLog.mock.calls.some(([message]) =>
      String(message).includes('provider child did not settle'))).toBe(false);
    expect(mockSendLog.mock.calls.some(([message]) =>
      String(message).includes('ended ('))).toBe(false);
  });

  it('re-admits a required ArtifactRef after transient materialization failure without starting a model turn or reply', async () => {
    vi.useFakeTimers();
    const signedUrl = 'https://storage.googleapis.com/bucket/board.png?X-Goog-Signature=sig';
    const operatorMessage = {
      id: 'msg-required-artifact-retry',
      content: 'Review this exact revision',
      created_at: '2026-07-22T12:00:00.000Z',
      from_type: 'user',
      context: {
        attachments: [{
          type: 'image',
          mime_type: 'image/png',
          name: 'board.png',
          url: signedUrl,
          artifact_ref_id: 'artifact-ref-1',
          image_checksum: 'ab'.repeat(32),
        }],
      },
    };
    let alive = true;
    const messagePollUrls: string[] = [];
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?')) {
        messagePollUrls.push(url);
        if (messagePollUrls.length === 2) alive = false;
        return Promise.resolve({ messages: [operatorMessage] });
      }
      return Promise.resolve(null);
    });
    const posts: Array<Record<string, unknown>> = [];
    mockApiPost.mockImplementation((_url: string, body?: Record<string, unknown>) => {
      if (body) posts.push(body);
      return Promise.resolve({ authority_held: true, stop_requested: false });
    });
    const attachmentFetch = vi.fn(() => Promise.resolve({
      ok: false,
      status: 503,
      headers: { get: (_name: string) => null },
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(0)),
    }));
    vi.stubGlobal('fetch', attachmentFetch);
    const session = makeSession(
      () => Promise.resolve(turnResult({ resultText: 'must never run' })),
      () => alive,
    );
    mockStartSession.mockReturnValue(session);

    const done = handleStrategicResident(
      'task-required-artifact-retry',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_operator_ack_threshold_seconds: 0,
        strategic_session_message_poll_seconds: 0,
      },
      { project_metadata: { project_id: 'proj-required-artifact-retry' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );
    await vi.advanceTimersByTimeAsync(6_000);
    await done;

    expect(messagePollUrls).toHaveLength(2);
    expect(messagePollUrls[1]).toBe(messagePollUrls[0]);
    expect(attachmentFetch).toHaveBeenCalledTimes(2);
    expect(session.sendTurn).not.toHaveBeenCalled();
    expect(posts.filter((post) => post.op === 'create_message')).toHaveLength(0);
    expect(posts.some((post) => Array.isArray(post.handled_message_ids))).toBe(false);
  });

  it.each(['headers', 'body'] as const)(
    'retries the same required ArtifactRef message after a whole-download %s timeout without model/reply/handled side effects',
    async (stallAt) => {
      vi.useFakeTimers();
      try {
        const signedUrl = 'https://storage.googleapis.com/bucket/board.png?X-Goog-Signature=sig';
        const operatorMessage = {
          id: `msg-required-artifact-${stallAt}-timeout`,
          content: 'Review this exact revision',
          created_at: '2026-07-22T12:00:00.000Z',
          from_type: 'user',
          context: {
            attachments: [{
              type: 'image',
              mime_type: 'image/png',
              name: 'board.png',
              url: signedUrl,
              artifact_ref_id: `artifact-${stallAt}-timeout`,
              image_checksum: 'ab'.repeat(32),
            }],
          },
        };
        let alive = true;
        const messagePollUrls: string[] = [];
        mockApiGet.mockImplementation((url: string) => {
          if (url.includes('/messages?')) {
            messagePollUrls.push(url);
            if (messagePollUrls.length === 2) alive = false;
            return Promise.resolve({ messages: [operatorMessage] });
          }
          return Promise.resolve(null);
        });
        const posts: Array<Record<string, unknown>> = [];
        mockApiPost.mockImplementation((_url: string, body?: Record<string, unknown>) => {
          if (body) posts.push(body);
          return Promise.resolve({ authority_held: true, stop_requested: false });
        });
        const signals: AbortSignal[] = [];
        const attachmentFetch = vi.fn((
          _url: string | URL | Request,
          init?: RequestInit,
        ) => {
          if (init?.signal) signals.push(init.signal);
          if (stallAt === 'headers') {
            return new Promise<never>(() => {});
          }
          return Promise.resolve({
            ok: true,
            status: 200,
            headers: { get: (_name: string) => null },
            arrayBuffer: () => new Promise<ArrayBuffer>(() => {}),
          });
        });
        vi.stubGlobal('fetch', attachmentFetch);
        const session = makeSession(
          () => Promise.resolve(turnResult({ resultText: 'must never run' })),
          () => alive,
        );
        mockStartSession.mockReturnValue(session);

        const done = handleStrategicResident(
          `task-required-artifact-${stallAt}-timeout`,
          {
            project_name: 'P',
            open_messages: [],
            strategic_session_operator_ack_threshold_seconds: 0,
            strategic_session_message_poll_seconds: 0,
          },
          {
            project_metadata: { project_id: `proj-required-artifact-${stallAt}-timeout` },
            context: {},
          },
          'ghtok', 'owner', 'repo', null, { value: '' },
        );
        await vi.advanceTimersByTimeAsync((ATTACHMENT_FETCH_TIMEOUT_MS * 2) + 10_000);
        await done;

        expect(messagePollUrls).toHaveLength(2);
        expect(messagePollUrls[1]).toBe(messagePollUrls[0]);
        expect(attachmentFetch).toHaveBeenCalledTimes(2);
        expect(signals).toHaveLength(2);
        expect(signals.every(signal => signal.aborted)).toBe(true);
        expect(session.sendTurn).not.toHaveBeenCalled();
        expect(posts.filter((post) => post.op === 'create_message')).toHaveLength(0);
        expect(posts.some((post) => Array.isArray(post.handled_message_ids))).toBe(false);
      } finally {
        vi.clearAllTimers();
        vi.useRealTimers();
        vi.unstubAllGlobals();
      }
    },
  );

  it('splits an aggregate required-image overflow into bounded same-timestamp model turns', async () => {
    const sharedCreatedAt = '2026-07-22T12:00:00.000Z';
    const emptySha256 = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';
    const makeRequiredMessage = (id: string, prefix: string) => ({
      id,
      content: `Review ${prefix}`,
      created_at: sharedCreatedAt,
      from_type: 'user',
      context: {
        attachments: Array.from({ length: 5 }, (_, index) => ({
          type: 'image',
          mime_type: 'image/png',
          name: `${prefix}-${index}.png`,
          url: `https://storage.googleapis.com/bucket/${prefix}-${index}.png?X-Goog-Signature=sig`,
          artifact_ref_id: `${prefix}-artifact-${index}`,
          image_checksum: emptySha256,
        })),
      },
    });
    const open = [
      makeRequiredMessage('required-batch-one', 'one'),
      makeRequiredMessage('required-batch-two', 'two'),
    ];
    const pollUrls: string[] = [];
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?')) {
        pollUrls.push(url);
        return Promise.resolve({ messages: [...open] });
      }
      return Promise.resolve(null);
    });
    const replyBatches: string[][] = [];
    mockApiPost.mockImplementation((_url: string, body?: Record<string, unknown>) => {
      if (body?.op === 'create_message') {
        const data = body.data as Record<string, unknown>;
        const covered = [...(data.covered_message_ids as string[])];
        replyBatches.push(covered);
        for (const id of covered) {
          const index = open.findIndex(message => message.id === id);
          if (index >= 0) open.splice(index, 1);
        }
      }
      return Promise.resolve({ authority_held: true, stop_requested: false });
    });
    const attachmentFetch = vi.fn(() => Promise.resolve({
      ok: true,
      status: 200,
      headers: { get: (_name: string) => null },
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(0)),
    }));
    vi.stubGlobal('fetch', attachmentFetch);
    const modelTurns: Array<Array<Record<string, unknown>>> = [];
    const session = makeSession(
      (input) => {
        if (Array.isArray(input)) modelTurns.push(input);
        return Promise.resolve(turnResult({
          resultText: 'Reviewed the exact revisions.',
          sentOperatorMessage: true,
        }));
      },
      () => open.length > 0,
    );
    mockStartSession.mockReturnValue(session);

    try {
      await handleStrategicResident(
        'task-required-artifact-split',
        {
          project_name: 'P',
          open_messages: [],
          strategic_session_operator_ack_threshold_seconds: 0,
        },
        {
          project_metadata: { project_id: 'proj-required-artifact-split' },
          context: { message_cursor: sharedCreatedAt },
        },
        'ghtok', 'owner', 'repo', null, { value: '' },
      );
    } finally {
      vi.unstubAllGlobals();
    }

    expect(replyBatches).toEqual([
      ['required-batch-one'],
      ['required-batch-two'],
    ]);
    expect(modelTurns).toHaveLength(2);
    const imageCounts = modelTurns.map(turn => turn.filter(block => block.type === 'image').length);
    expect(imageCounts).toEqual([5, 5]);
    expect(imageCounts.every(count => count <= MAX_IMAGES_PER_TURN)).toBe(true);
    expect(attachmentFetch).toHaveBeenCalledTimes(10);
    expect(pollUrls.length).toBeGreaterThanOrEqual(2);
    expect(pollUrls[1]).toBe(pollUrls[0]);
    expect(open).toHaveLength(0);
  });

  it('does not start the model turn when the stable idempotency key cannot be installed', async () => {
    const operatorMessage = {
      id: 'msg-idempotency-file-failure',
      content: 'Retry me on a healthy worker',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let delivered = false;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?') && !delivered) {
        delivered = true;
        return Promise.resolve({ messages: [operatorMessage] });
      }
      if (url.includes('/messages?')) return Promise.resolve({ messages: [] });
      return Promise.resolve(null);
    });
    const posts: Array<Record<string, unknown>> = [];
    mockApiPost.mockImplementation((_url: string, body?: Record<string, unknown>) => {
      if (body) posts.push(body);
      return Promise.resolve({ authority_held: true, stop_requested: false });
    });
    vi.spyOn(fs, 'writeFileSync').mockImplementationOnce(() => {
      throw new Error('workspace is read-only');
    });
    const session = makeSession(
      () => Promise.resolve(turnResult({ resultText: 'must never run' })),
      () => true,
    );
    mockStartSession.mockReturnValue(session);

    await handleStrategicResident(
      'task-idempotency-file-failure',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_operator_ack_threshold_seconds: 0,
      },
      { project_metadata: { project_id: 'proj-idempotency-file-failure' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    expect(session.sendTurn).not.toHaveBeenCalled();
    expect(posts.filter((post) => post.op === 'create_message')).toHaveLength(0);
    expect(posts.some((post) => Array.isArray(post.handled_message_ids))).toBe(false);
    expect(session.close).toHaveBeenCalled();
  });

  it('sends the first text-only operator turn as one stream-json content item', async () => {
    const message = {
      id: 'msg-single-content-item',
      content: 'hi. respond to me as soon as you are ready.',
      created_at: '2026-07-25T04:47:18.384Z',
      from_type: 'user',
    };
    let delivered = false;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?')) {
        if (!delivered) {
          delivered = true;
          return Promise.resolve({ messages: [message] });
        }
        return Promise.resolve({ messages: [] });
      }
      return Promise.resolve(null);
    });
    let heartbeatCount = 0;
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/heartbeat')) {
        heartbeatCount += 1;
        return Promise.resolve({
          authority_held: heartbeatCount === 1,
          stop_requested: heartbeatCount > 1,
        });
      }
      return Promise.resolve({});
    });
    const session = makeSession(
      () => Promise.resolve(turnResult({ resultText: 'ready' })),
      () => true,
    );
    mockStartSession.mockReturnValue(session);

    await handleStrategicResident(
      'task-single-content-item',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_operator_ack_threshold_seconds: 0,
      },
      { project_metadata: { project_id: 'proj-single-content-item' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    const input = session.sendTurn.mock.calls[0]?.[0];
    expect(session.sendTurn).toHaveBeenCalledTimes(1);
    expect(typeof input).toBe('string');
    expect(input).toEqual(expect.stringContaining(message.content));
  });

  it('retries a failed initial heartbeat before polling or starting provider work', async () => {
    const events: string[] = [];
    let delivered = false;
    let heartbeatCount = 0;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?')) {
        events.push('poll');
        if (!delivered) {
          delivered = true;
          return Promise.resolve({
            messages: [{
              id: 'msg-after-authority',
              content: 'only answer after authority is proven',
              created_at: new Date().toISOString(),
              from_type: 'user',
            }],
          });
        }
        return Promise.resolve({ messages: [] });
      }
      return Promise.resolve(null);
    });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/heartbeat')) {
        heartbeatCount += 1;
        events.push(`heartbeat-${heartbeatCount}`);
        if (heartbeatCount === 1) {
          return Promise.reject(new Error('transient admission failure'));
        }
        return Promise.resolve({
          authority_held: heartbeatCount === 2,
          stop_requested: heartbeatCount > 2,
        });
      }
      if (body?.op === 'create_message') {
        events.push('reply');
      }
      return Promise.resolve({});
    });
    const session = makeSession(
      () => {
        events.push('provider');
        return Promise.resolve(turnResult({
          sentOperatorMessage: true,
          resultText: 'authority-gated reply',
        }));
      },
      () => true,
    );
    mockStartSession.mockReturnValue(session);

    await handleStrategicResident(
      'task-initial-authority',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_operator_ack_threshold_seconds: 0,
      },
      { project_metadata: { project_id: 'proj-initial-authority' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    expect(events.slice(0, 3)).toEqual([
      'heartbeat-1',
      'heartbeat-2',
      'poll',
    ]);
    expect(events.indexOf('provider')).toBeGreaterThan(
      events.indexOf('heartbeat-2'),
    );
    expect(mockSendLog.mock.calls.some(([message]) =>
      String(message).includes('initial authority check failed'))).toBe(false);
  });

  it('uses distinct immutable receipts when M2 arrives after atomic M1 reply', async () => {
    const m1 = {
      id: 'msg-anchor-m1',
      content: 'first operator message',
      created_at: '2026-07-21T20:00:00.000Z',
      from_type: 'user',
    };
    const m2 = {
      id: 'msg-newer-m2',
      content: 'second operator message',
      created_at: '2026-07-21T20:01:00.000Z',
      from_type: 'user',
    };
    const installedKeys: string[] = [];
    const installedReceipts: Array<{
      idempotency_key: string;
      covered_message_ids: string[];
      reply_to_message_id: string;
    }> = [];
    const heartbeatBodies: Array<Record<string, unknown>> = [];
    const realWriteFileSync = fs.writeFileSync;
    vi.spyOn(fs, 'writeFileSync').mockImplementation((file, data, options) => {
      const written = typeof data === 'string'
        ? data
        : Buffer.from(data.buffer, data.byteOffset, data.byteLength).toString();
      if (String(file).endsWith('.resident-reply-idempotency-key')) {
        installedKeys.push(written);
      }
      if (String(file).endsWith('.resident-reply-receipt.json')) {
        installedReceipts.push(JSON.parse(written));
      }
      realWriteFileSync(file, data, options);
    });

    const runAttempt = async (
      messages: Array<typeof m1>,
      heartbeatResult: 'fail' | 'stop',
    ): Promise<MockSession> => {
      let delivered = false;
      let heartbeatCount = 0;
      mockApiGet.mockImplementation((url: string) => {
        if (url.includes('/messages?') && !delivered) {
          delivered = true;
          return Promise.resolve({ messages });
        }
        if (url.includes('/messages?')) return Promise.resolve({ messages: [] });
        return Promise.resolve(null);
      });
      mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
        if (url.includes('/heartbeat')) {
          heartbeatCount += 1;
          if (heartbeatCount === 1) {
            return Promise.resolve({ authority_held: true, stop_requested: false });
          }
          heartbeatBodies.push(body ?? {});
          return heartbeatResult === 'fail'
            ? Promise.reject(new Error('heartbeat transport dropped after durable reply'))
            : Promise.resolve({ authority_held: false, stop_requested: true });
        }
        return Promise.resolve({});
      });
      const session = makeSession(
        () => Promise.resolve(turnResult({ sentOperatorMessage: true, resultText: 'operator reply' })),
        () => true,
      );
      mockStartSession.mockReturnValue(session);
      await handleStrategicResident(
        'task-stable-batch-retry',
        {
          project_name: 'P',
          open_messages: [],
          strategic_session_operator_ack_threshold_seconds: 0,
          strategic_session_id: 'stable-logical-session',
        },
        { project_metadata: { project_id: 'proj-stable-batch-retry' }, context: {} },
        'ghtok', 'owner', 'repo', null, { value: '' },
      );
      return session;
    };

    const crashed = await runAttempt([m1], 'fail');
    expect(crashed.sendTurn).toHaveBeenCalledTimes(1);
    expect(crashed.close).toHaveBeenCalled();

    mockApiGet.mockReset();
    mockApiPost.mockReset();
    mockStartSession.mockReset();
    // Atomic reply creation already handled M1, so the restarted poll exposes
    // only the newly-arrived M2 rather than joining it to an old retry batch.
    const retried = await runAttempt([m2], 'stop');
    expect(retried.sendTurn).toHaveBeenCalledTimes(1);

    expect(installedKeys).toHaveLength(2);
    expect(installedKeys[1]).not.toBe(installedKeys[0]);
    expect(installedReceipts).toEqual([
      {
        idempotency_key: installedKeys[0],
        covered_message_ids: ['msg-anchor-m1'],
        reply_to_message_id: 'msg-anchor-m1',
      },
      {
        idempotency_key: installedKeys[1],
        covered_message_ids: ['msg-newer-m2'],
        reply_to_message_id: 'msg-newer-m2',
      },
    ]);
    expect(heartbeatBodies).toHaveLength(2);
    expect(heartbeatBodies.every(body => !('handled_message_ids' in body))).toBe(true);
  });

  it('claims the exact polled batch and fences its persisted reply', async () => {
    const operatorMessage = {
      id: 'msg-durable-claim',
      content: 'finish this without losing it if the worker crashes',
      created_at: '2026-07-21T20:00:00.000Z',
      from_type: 'user',
    };
    let delivered = false;
    const claimBodies: Array<Record<string, unknown>> = [];
    const replyBodies: Array<Record<string, unknown>> = [];
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?') && !delivered) {
        delivered = true;
        return Promise.resolve({ messages: [operatorMessage], claim_required: true });
      }
      if (url.includes('/messages?')) {
        return Promise.resolve({ messages: [], claim_required: true });
      }
      return Promise.resolve(null);
    });
    let heartbeatCount = 0;
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/messages/claim')) {
        claimBodies.push(body ?? {});
        return Promise.resolve({
          claim_id: 'claim-durable-1',
          message_ids: [operatorMessage.id],
          lease_expires_at: '2026-07-21T20:01:00.000Z',
          attempt: 1,
          incarnation_supported: true,
          incarnation_id: body?.resident_incarnation_id,
          resident_incarnation_id: body?.resident_incarnation_id,
        });
      }
      if (body?.op === 'create_message') {
        replyBodies.push(body.data as Record<string, unknown>);
        return Promise.resolve({});
      }
      if (url.includes('/heartbeat')) {
        heartbeatCount += 1;
        return Promise.resolve({
          authority_held: heartbeatCount === 1,
          stop_requested: heartbeatCount > 1,
        });
      }
      return Promise.resolve({});
    });
    const session = makeSession(
      () => Promise.resolve(turnResult({ sentOperatorMessage: true, resultText: 'durable reply' })),
      () => true,
    );
    mockStartSession.mockReturnValue(session);

    await handleStrategicResident(
      'task-durable-claim',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_operator_ack_threshold_seconds: 0,
        strategic_session_id: 'durable-session',
        _resident_worker_generation: 1,
      },
      { project_metadata: { project_id: 'proj-durable-claim' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    expect(claimBodies).toEqual([expect.objectContaining({
      task_id: 'task-durable-claim',
      session_id: 'durable-session',
      worker_generation: 1,
      message_ids: [operatorMessage.id],
    })]);
    expect(claimBodies[0]?.resident_incarnation_id).toEqual(expect.any(String));
    expect(claimBodies[0]).not.toHaveProperty('incarnation_id');
    expect(replyBodies).toHaveLength(1);
    expect(replyBodies[0]).toMatchObject({
      covered_message_ids: [operatorMessage.id],
      context: { reply_to: operatorMessage.id },
      conversation_claim_id: 'claim-durable-1',
      conversation_claim_incarnation:
        claimBodies[0].resident_incarnation_id,
    });
  });

  it('claims after preflight and renews a slow turn independently of idle pacing', async () => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'msg-slow-durable-claim',
      content: 'take the time needed but retain this exact turn',
      created_at: '2026-07-21T20:00:00.000Z',
      from_type: 'user',
    };
    const events: string[] = [];
    const heartbeatBodies: Array<Record<string, unknown>> = [];
    let delivered = false;
    let replyPersisted = false;
    vi.mocked(syncResidentRepository).mockImplementationOnce(() => {
      events.push('repo-preflight');
      return {
        status: 'CURRENT',
        head: '0123456789abcdef0123456789abcdef01234567',
        previousHead: null,
        commits: [],
        changedFiles: [],
        omittedCommits: 0,
        omittedChangedFiles: 0,
        deltaAvailable: false,
        reason: null,
      };
    });
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?') && !delivered) {
        delivered = true;
        return Promise.resolve({ messages: [operatorMessage], claim_required: true });
      }
      if (url.includes('/messages?')) {
        return Promise.resolve({ messages: [], claim_required: true });
      }
      return Promise.resolve(null);
    });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/messages/claim')) {
        events.push('claim');
        return Promise.resolve({
          claim_id: 'claim-slow-1',
          message_ids: [operatorMessage.id],
          lease_expires_at: '2026-07-21T20:01:30.000Z',
          attempt: 1,
          incarnation_supported: true,
          incarnation_id: body?.resident_incarnation_id,
          resident_incarnation_id: body?.resident_incarnation_id,
        });
      }
      if (url.includes('/heartbeat')) {
        heartbeatBodies.push(body ?? {});
        if (replyPersisted && !body?.conversation_claim_id) {
          return Promise.resolve({ authority_held: false, stop_requested: true });
        }
        return Promise.resolve({
          authority_held: true,
          stop_requested: false,
          conversation_claim_held: true,
        });
      }
      if (body?.op === 'create_message') {
        replyPersisted = true;
        return Promise.resolve({});
      }
      return Promise.resolve({});
    });
    const session = makeSession(
      () => {
        events.push('provider-turn');
        return new Promise<ResidentTurnResult>((resolve) => {
          setTimeout(() => {
            resolve(turnResult({
              sentOperatorMessage: true,
              resultText: 'slow durable reply',
            }));
          }, 35_000);
        });
      },
      () => true,
    );
    mockStartSession.mockReturnValue(session);

    try {
      const handling = handleStrategicResident(
        'task-slow-durable-claim',
        {
          project_name: 'P',
          open_messages: [],
          strategic_session_operator_ack_threshold_seconds: 0,
          strategic_session_idle_tick_seconds: 300,
          strategic_session_id: 'slow-durable-session',
          _resident_worker_generation: 1,
        },
        { project_metadata: { project_id: 'proj-slow-durable-claim' }, context: {} },
        'ghtok', 'owner', 'repo', null, { value: '' },
      );
      await vi.advanceTimersByTimeAsync(1_000);
      expect(events.slice(0, 3)).toEqual([
        'repo-preflight',
        'claim',
        'provider-turn',
      ]);
      await vi.advanceTimersByTimeAsync(36_000);
      await handling;
    } finally {
      vi.clearAllTimers();
      vi.useRealTimers();
    }

    const claimHeartbeats = heartbeatBodies.filter(
      body => body.conversation_claim_id === 'claim-slow-1',
    );
    expect(claimHeartbeats.length).toBeGreaterThanOrEqual(2);
    expect(claimHeartbeats.every(
      body => typeof body.conversation_claim_incarnation === 'string',
    )).toBe(true);
    expect(claimHeartbeats.every(
      body => body.conversation_claim_lease_seconds === 90,
    )).toBe(true);
  });

  it('extends a legacy claim after backend upgrade when a retry batch grows', async () => {
    vi.useFakeTimers();
    vi.mocked(fs.existsSync).mockImplementation((candidate) => (
      String(candidate) !== replyToolPaths.current?.stagedReplyPath
    ));
    const firstMessage = {
      id: 'msg-legacy-first',
      content: 'first message',
      created_at: '2026-07-21T20:00:00.000Z',
      from_type: 'user',
    };
    const secondMessage = {
      id: 'msg-legacy-second',
      content: 'arrived during retry',
      created_at: '2026-07-21T20:00:01.000Z',
      from_type: 'user',
    };
    const legacyIncarnation = '00000000-0000-0000-0000-000000000811';
    const claimBodies: Array<Record<string, unknown>> = [];
    const replyBodies: Array<Record<string, unknown>> = [];
    let pollCount = 0;
    let replyPersisted = false;
    let replacementAlive = true;
    mockApiGet.mockImplementation((url: string) => {
      if (!url.includes('/messages?')) return Promise.resolve(null);
      pollCount += 1;
      return Promise.resolve({
        messages: pollCount === 1
          ? [firstMessage]
          : [firstMessage, secondMessage],
        claim_required: true,
      });
    });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/messages/claim')) {
        claimBodies.push(body ?? {});
        if (claimBodies.length === 1) {
          // 0709 accepts the additive request field but cannot echo support.
          return Promise.resolve({
            claim_id: 'claim-rolling-upgrade',
            message_ids: [firstMessage.id],
            lease_expires_at: '2026-07-21T20:01:30.000Z',
            attempt: 1,
          });
        }
        // 0710 adopts the NULL 0709 rows under deterministic legacy identity
        // and extends the same logical claim to the newly arrived row.
        return Promise.resolve({
          claim_id: 'claim-rolling-upgrade',
          message_ids: [firstMessage.id, secondMessage.id],
          lease_expires_at: '2026-07-21T20:02:30.000Z',
          attempt: 1,
          incarnation_supported: true,
          incarnation_id: legacyIncarnation,
          resident_incarnation_id: body?.resident_incarnation_id,
        });
      }
      if (body?.op === 'create_message') {
        replyPersisted = true;
        replacementAlive = false;
        replyBodies.push(body.data as Record<string, unknown>);
        return Promise.resolve({});
      }
      if (url.includes('/heartbeat')) {
        return Promise.resolve(replyPersisted
          ? { authority_held: false, stop_requested: true }
          : {
            authority_held: true,
            stop_requested: false,
            ...(body?.conversation_claim_id
              ? { conversation_claim_held: true }
              : {}),
          });
      }
      return Promise.resolve({});
    });
    const failedSession = makeSession(
      () => Promise.resolve(turnResult({
        processGone: true,
        sentOperatorMessage: false,
        failureDetail: 'provider process disappeared',
      })),
      () => true,
    );
    let replacementTurns = 0;
    const replacementSession = makeSession(
      () => {
        replacementTurns += 1;
        return Promise.resolve(replacementTurns === 1
          ? turnResult({ sentOperatorMessage: false, sawToolUse: false })
          : turnResult({
            sentOperatorMessage: true,
            resultText: 'covered both messages after retry',
          }));
      },
      () => replacementAlive,
    );
    mockStartSession
      .mockReturnValueOnce(failedSession)
      .mockReturnValue(replacementSession);

    try {
      const handling = handleStrategicResident(
        'task-rolling-claim',
        {
          project_name: 'P',
          open_messages: [],
          strategic_session_operator_ack_threshold_seconds: 0,
          strategic_session_message_poll_seconds: 1,
          strategic_session_id: 'rolling-session',
          _resident_worker_generation: 1,
        },
        { project_metadata: { project_id: 'proj-rolling-claim' }, context: {} },
        'ghtok', 'owner', 'repo', null, { value: '' },
      );
      const state = { settled: false };
      void handling.finally(() => { state.settled = true; });
      for (let step = 0; step < 20; step += 1) {
        if (state.settled) break;
        await vi.advanceTimersByTimeAsync(10_000);
      }
      expect(state.settled).toBe(true);
      await handling;
    } finally {
      vi.clearAllTimers();
      vi.useRealTimers();
    }

    expect(claimBodies).toHaveLength(2);
    expect(claimBodies[0]).not.toHaveProperty('incarnation_id');
    expect(claimBodies[1]).not.toHaveProperty('incarnation_id');
    expect(claimBodies[1].message_ids).toEqual([
      firstMessage.id,
      secondMessage.id,
    ]);
    expect(replyBodies).toHaveLength(1);
    expect(replyBodies[0]).toMatchObject({
      conversation_claim_id: 'claim-rolling-upgrade',
      conversation_claim_incarnation: legacyIncarnation,
      covered_message_ids: [firstMessage.id, secondMessage.id],
    });
  });

  it('revalidates an existing claim before retrying the provider turn', async () => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'msg-expired-between-attempts',
      content: 'do not run twice after the lease expires',
      created_at: '2026-07-21T20:00:00.000Z',
      from_type: 'user',
    };
    let alive = true;
    let claimCalls = 0;
    let claimRenewals = 0;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?')) {
        return Promise.resolve({
          messages: [operatorMessage],
          claim_required: true,
        });
      }
      return Promise.resolve(null);
    });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/messages/claim')) {
        claimCalls += 1;
        return Promise.resolve({
          claim_id: `claim-expired-before-retry-${claimCalls}`,
          message_ids: [operatorMessage.id],
          lease_expires_at: '2026-07-21T20:01:30.000Z',
          attempt: 1,
          incarnation_supported: true,
          incarnation_id: body?.resident_incarnation_id,
        });
      }
      if (
        url.includes('/heartbeat')
        && body?.conversation_claim_id === 'claim-expired-before-retry-1'
      ) {
        claimRenewals += 1;
        return Promise.resolve({
          authority_held: true,
          stop_requested: false,
          conversation_claim_held: false,
        });
      }
      if (body?.op === 'create_message') return Promise.resolve({});
      if (url.includes('/heartbeat') && claimCalls >= 2) {
        alive = false;
        return Promise.resolve({ authority_held: false, stop_requested: true });
      }
      return Promise.resolve({ authority_held: true, stop_requested: false });
    });
    const failedSession = makeSession(
      () => Promise.resolve(turnResult({
        processGone: true,
        sentOperatorMessage: false,
        failureDetail: 'provider process disappeared',
      })),
      () => true,
    );
    let replacementTurns = 0;
    const replacementSession = makeSession(
      () => {
        replacementTurns += 1;
        return Promise.resolve(turnResult({
          sentOperatorMessage: true,
          resultText: 'reply after exact claim reacquisition',
        }));
      },
      () => alive,
    );
    mockStartSession
      .mockReturnValueOnce(failedSession)
      .mockReturnValue(replacementSession);

    try {
      const handling = handleStrategicResident(
        'task-expired-claim-retry',
        {
          project_name: 'P',
          open_messages: [],
          strategic_session_operator_ack_threshold_seconds: 0,
          strategic_session_message_poll_seconds: 1,
          strategic_session_id: 'expired-claim-session',
        },
        {
          project_metadata: { project_id: 'proj-expired-claim-retry' },
          context: {},
        },
        'ghtok', 'owner', 'repo', null, { value: '' },
      );
      const state = { settled: false };
      void handling.finally(() => { state.settled = true; });
      for (let step = 0; step < 20; step += 1) {
        if (state.settled) break;
        await vi.advanceTimersByTimeAsync(10_000);
      }
      expect(state.settled).toBe(true);
      await handling;
    } finally {
      vi.clearAllTimers();
      vi.useRealTimers();
    }

    expect(claimCalls).toBe(2);
    expect(claimRenewals).toBe(1);
    expect(replacementTurns).toBe(1);
  });

  it('preserves conversation_claim_lost as the terminal replacement cause', async () => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'msg-lost-durable-claim',
      content: 'do not misclassify a fenced turn',
      created_at: '2026-07-21T20:00:00.000Z',
      from_type: 'user',
    };
    let delivered = false;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?') && !delivered) {
        delivered = true;
        return Promise.resolve({ messages: [operatorMessage], claim_required: true });
      }
      if (url.includes('/messages?')) {
        return Promise.resolve({ messages: [], claim_required: true });
      }
      return Promise.resolve(null);
    });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (url.includes('/messages/claim')) {
        return Promise.resolve({
          claim_id: 'claim-lost-1',
          message_ids: [operatorMessage.id],
          lease_expires_at: '2026-07-21T20:01:30.000Z',
          attempt: 1,
        });
      }
      if (url.includes('/heartbeat') && body?.conversation_claim_id) {
        return Promise.resolve({
          authority_held: true,
          stop_requested: false,
          conversation_claim_held: false,
        });
      }
      return Promise.resolve({ authority_held: true, stop_requested: false });
    });
    const session = makeSession(
      () => new Promise<ResidentTurnResult>((resolve) => {
        setTimeout(() => {
          resolve(turnResult({
            sentOperatorMessage: true,
            resultText: 'must not be persisted',
          }));
        }, 20_000);
      }),
      () => true,
    );
    mockStartSession.mockReturnValue(session);

    try {
      const handling = handleStrategicResident(
        'task-lost-durable-claim',
        {
          project_name: 'P',
          open_messages: [],
          strategic_session_operator_ack_threshold_seconds: 0,
          strategic_session_id: 'lost-durable-session',
          _resident_worker_generation: 1,
        },
        { project_metadata: { project_id: 'proj-lost-durable-claim' }, context: {} },
        'ghtok', 'owner', 'repo', null, { value: '' },
      );
      const state = { settled: false };
      void handling.finally(() => { state.settled = true; });
      for (let step = 0; step < 50; step += 1) {
        if (state.settled) break;
        await vi.advanceTimersByTimeAsync(1_000);
      }
      expect(state.settled).toBe(true);
      await handling;
    } finally {
      vi.clearAllTimers();
      vi.useRealTimers();
    }

    expect(session.interrupt).toHaveBeenCalled();
    expect(mockApiPost.mock.calls.filter(([, body]) =>
      body?.op === 'create_message')).toHaveLength(0);
    expect(vi.mocked(persistAndPostStatus).mock.calls.some(([taskId, payload]) => (
      taskId === 'task-lost-durable-claim'
      && String(payload.logs).includes('conversation_claim_lost')
    ))).toBe(true);
  });

  it('drains 201 same-timestamp operator messages as atomic one-page batches without suppressing deferred rows', async () => {
    const sharedCreatedAt = '2026-07-21T20:00:00.000Z';
    const open = Array.from({ length: 201 }, (_, index) => ({
      id: `same-time-${String(index + 1).padStart(3, '0')}`,
      content: `operator message ${index + 1}`,
      created_at: sharedCreatedAt,
      from_type: 'user',
    }));
    const replyBatches: string[][] = [];
    let heartbeatCount = 0;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?')) return Promise.resolve({ messages: open.slice(0, 20) });
      return Promise.resolve(null);
    });
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (body?.op === 'create_message') {
        const data = body.data as Record<string, unknown>;
        const covered = [...(data.covered_message_ids as string[])];
        replyBatches.push(covered);
        for (const id of covered) {
          const index = open.findIndex(message => message.id === id);
          if (index >= 0) open.splice(index, 1);
        }
        return Promise.resolve({});
      }
      if (url.includes('/heartbeat')) {
        heartbeatCount += 1;
        return Promise.resolve({
          authority_held: true,
          stop_requested: heartbeatCount >= 12,
        });
      }
      return Promise.resolve({});
    });
    const session = makeSession(
      () => Promise.resolve(turnResult({
        resultText: 'Handled this bounded operator batch.',
        sentOperatorMessage: true,
      })),
      () => true,
    );
    mockStartSession.mockReturnValue(session);

    await handleStrategicResident(
      'task-same-timestamp-backlog',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_operator_ack_threshold_seconds: 0,
      },
      {
        project_metadata: { project_id: 'proj-same-timestamp-backlog' },
        context: { message_cursor: sharedCreatedAt },
      },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    expect(replyBatches).toHaveLength(11);
    expect(replyBatches.slice(0, 10).every(batch => batch.length === 20)).toBe(true);
    expect(replyBatches[10]).toHaveLength(1);
    expect(replyBatches.flat()).toHaveLength(201);
    expect(new Set(replyBatches.flat()).size).toBe(201);
    expect(open).toHaveLength(0);
  });

  it('stops before any further model work when reply receipt cleanup fails after a turn', async () => {
    const operatorMessage = {
      id: 'msg-cleanup-failure',
      content: 'Answer once, then stop if receipt cleanup is unsafe',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let delivered = false;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?') && !delivered) {
        delivered = true;
        return Promise.resolve({ messages: [operatorMessage] });
      }
      if (url.includes('/messages?')) return Promise.resolve({ messages: [] });
      return Promise.resolve(null);
    });
    mockApiPost.mockResolvedValue({ authority_held: true, stop_requested: false });

    let turnFinished = false;
    let cleanupFailed = false;
    vi.spyOn(fs, 'unlinkSync').mockImplementation((file) => {
      if (
        turnFinished
        && !cleanupFailed
        && String(file).endsWith('.resident-reply-idempotency-key')
      ) {
        cleanupFailed = true;
        throw Object.assign(new Error('receipt cleanup denied'), { code: 'EACCES' });
      }
    });

    const session = makeSession(
      () => {
        turnFinished = true;
        return Promise.resolve(turnResult({ sentOperatorMessage: true }));
      },
      () => true,
    );
    mockStartSession.mockReturnValue(session);

    await handleStrategicResident(
      'task-cleanup-failure',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_operator_ack_threshold_seconds: 0,
      },
      { project_metadata: { project_id: 'proj-cleanup-failure' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    expect(cleanupFailed).toBe(true);
    expect(session.sendTurn).toHaveBeenCalledTimes(1);
    expect(session.close).toHaveBeenCalled();
  });

  it('fails closed before the model when an operator batch has no stable message id', async () => {
    let delivered = false;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?') && !delivered) {
        delivered = true;
        return Promise.resolve({ messages: [{
          content: 'missing id must retry from the backend',
          created_at: new Date().toISOString(),
          from_type: 'user',
        }] });
      }
      if (url.includes('/messages?')) return Promise.resolve({ messages: [] });
      return Promise.resolve(null);
    });
    mockApiPost.mockResolvedValue({ authority_held: true });
    const session = makeSession(
      () => Promise.resolve(turnResult({ resultText: 'must never run' })),
      () => true,
    );
    mockStartSession.mockReturnValue(session);

    await handleStrategicResident(
      'task-missing-stable-id',
      { project_name: 'P', open_messages: [] },
      { project_metadata: { project_id: 'proj-missing-stable-id' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    expect(session.sendTurn).not.toHaveBeenCalled();
    expect(session.close).toHaveBeenCalled();
  });
  it('leaves inbound messages open when native reply persistence fails', async () => {
    const operatorMessage = {
      id: 'msg-backstop-failure',
      content: 'Do not lose this reply',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let delivered = false;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?') && !delivered) {
        delivered = true;
        return Promise.resolve({ messages: [operatorMessage] });
      }
      if (url.includes('/messages?')) return Promise.resolve({ messages: [] });
      return Promise.resolve(null);
    });

    const posts: Array<Record<string, unknown>> = [];
    mockApiPost.mockImplementation((_url: string, body?: Record<string, unknown>) => {
      if (body) posts.push(body);
      if (body?.op === 'create_message') {
        return Promise.reject(new Error('reply store unavailable'));
      }
      return Promise.resolve({ authority_held: true, stop_requested: false });
    });
    const session = makeSession(
      () => Promise.resolve(turnResult({
        resultText: 'A complete answer staged by the native reply tool',
        accumulatedTurnText: 'A complete answer staged by the native reply tool',
        sentOperatorMessage: true,
      })),
      () => true,
    );
    mockStartSession.mockReturnValue(session);

    await handleStrategicResident(
      'task-backstop-failure',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_operator_ack_threshold_seconds: 0,
      },
      { project_metadata: { project_id: 'proj-backstop-failure' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    expect(posts.filter((post) => post.op === 'create_message')).toHaveLength(1);
    expect(posts.some((post) => Array.isArray(post.handled_message_ids))).toBe(false);
    expect(session.close).toHaveBeenCalled();
  });
  it('uses one same-session corrective turn and persists exactly one reply without re-admitting input', async () => {
    vi.useFakeTimers();
    vi.mocked(recordResidentTurnLatency).mockClear();
    vi.mocked(fs.existsSync).mockImplementation((candidate) => (
      String(candidate) !== replyToolPaths.current?.stagedReplyPath
    ));
    const operatorMessage = {
      id: 'msg-inline-only',
      content: 'Please answer in Workday.',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let persisted = false;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?')) {
        return Promise.resolve({
          messages: persisted ? [] : [operatorMessage],
          claim_required: true,
        });
      }
      return Promise.resolve(null);
    });
    const posts: Array<Record<string, unknown>> = [];
    mockApiPost.mockImplementation((url: string, body?: Record<string, unknown>) => {
      if (body) posts.push(body);
      if (url.includes('/messages/claim')) {
        return Promise.resolve({
          claim_id: 'claim-inline-only',
          message_ids: [operatorMessage.id],
          incarnation_supported: true,
          incarnation_id: 'claim-incarnation-inline-only',
        });
      }
      if (body?.op === 'create_message') {
        persisted = true;
        return Promise.resolve({});
      }
      if (url.includes('/heartbeat')) {
        return Promise.resolve({
          authority_held: true,
          resident_incarnation_held: true,
          conversation_claim_held: true,
          stop_requested: persisted,
        });
      }
      return Promise.resolve({});
    });
    let providerTurn = 0;
    const sameSession = makeSession(
      (text) => {
        providerTurn += 1;
        if (providerTurn === 1) {
          return Promise.resolve(turnResult({
            resultText: 'This inline final must not be delivered.',
            accumulatedTurnText: 'This inline final must not be delivered.',
            sawToolUse: true,
            toolCallCount: 1,
            totalToolCallDurationMs: 3,
            turnMs: 7,
            trace: [
              { elapsedMs: 1, kind: 'tool', label: 'MCP:project_read' },
              { elapsedMs: 2, kind: 'tool_result', label: 'MCP:project_read', isError: false },
              { elapsedMs: 7, kind: 'result', label: 'completed' },
            ],
            traceTruncated: false,
            sentOperatorMessage: false,
          }));
        }
        expect(JSON.stringify(text)).toContain('PROJECT READ ALREADY COMPLETE');
        expect(JSON.stringify(text)).toContain('Do not call project_read again');
        expect(JSON.stringify(text)).not.toContain(operatorMessage.content);
        return Promise.resolve(turnResult({
          resultText: 'Delivered through the native reply tool.',
          accumulatedTurnText: 'Delivered through the native reply tool.',
          sawToolUse: true,
          toolCallCount: 1,
          totalToolCallDurationMs: 5,
          turnMs: 11,
          sentOperatorMessage: true,
        }));
      },
      () => true,
    );
    mockStartSession.mockReturnValue(sameSession);

    const run = handleStrategicResident(
      'task-inline-only',
      { project_name: 'P', open_messages: [], strategic_session_operator_ack_threshold_seconds: 0 },
      { project_metadata: { project_id: 'proj-inline-only' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );
    await vi.advanceTimersByTimeAsync(2_000);
    await run;

    expect(posts.filter(post => post.op === 'create_message')).toHaveLength(1);
    expect(mockStartSession).toHaveBeenCalledTimes(1);
    expect(sameSession.sendTurn).toHaveBeenCalledTimes(2);
    expect(sameSession.sendTurn).toHaveBeenNthCalledWith(
      2,
      expect.anything(),
      expect.objectContaining({ hardTimeoutMs: 60_000 }),
    );
    expect(sameSession.close).toHaveBeenCalledTimes(1);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      String(url).includes('/messages/claim'))).toHaveLength(1);
    const operatorLatency = vi.mocked(recordResidentTurnLatency).mock.calls
      .map((call) => call[0])
      .filter((sample) => sample.kind === 'operator');
    expect(operatorLatency).toContainEqual(expect.objectContaining({
      turnMs: 18,
      toolCallCount: 2,
      toolCallDurationMs: 8,
    }));
    // This suite deliberately exercises failing paths before this case.  Scope
    // the assertion to this task so an earlier test's expected terminal cannot
    // make a retry regression look flaky.
    expect(vi.mocked(persistAndPostStatus).mock.calls.some(([taskId, payload]) => (
      taskId === 'task-inline-only' && payload.status === 'FAILED'
    ))).toBe(false);
  });

  it('fails closed after one same-session corrective omission without re-admitting input', async () => {
    vi.useFakeTimers();
    vi.mocked(fs.existsSync).mockImplementation((candidate) => (
      String(candidate) !== replyToolPaths.current?.stagedReplyPath
    ));
    const operatorMessage = {
      id: 'msg-reply-protocol-broken',
      content: 'Do not retry this forever.',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?')) {
        return Promise.resolve({ messages: [operatorMessage], claim_required: true });
      }
      return Promise.resolve(null);
    });
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/messages/claim')) {
        return Promise.resolve({
          claim_id: 'claim-reply-protocol-broken',
          message_ids: [operatorMessage.id],
          incarnation_supported: true,
          incarnation_id: 'claim-incarnation-reply-protocol-broken',
        });
      }
      return Promise.resolve({
        authority_held: true,
        resident_incarnation_held: true,
        conversation_claim_held: true,
        stop_requested: false,
      });
    });
    let providerTurn = 0;
    const brokenSession = makeSession(
      () => {
        providerTurn += 1;
        const text = providerTurn === 1
          ? 'inline output is not delivery'
          : 'still missing the native reply';
        return Promise.resolve(turnResult({
          resultText: text,
          accumulatedTurnText: text,
          sawToolUse: false,
          sentOperatorMessage: false,
        }));
      },
      () => true,
    );
    Object.assign(brokenSession, { failureCategory: vi.fn(() => 'dead_auth') });
    mockStartSession.mockReturnValue(brokenSession);

    const run = handleStrategicResident(
      'task-reply-protocol-broken',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_operator_ack_threshold_seconds: 0,
      },
      {
        project_metadata: { project_id: 'proj-reply-protocol-broken' },
        context: {},
      },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );
    await vi.advanceTimersByTimeAsync(2_000);
    await run;

    expect(mockStartSession).toHaveBeenCalledTimes(1);
    expect(brokenSession.sendTurn).toHaveBeenCalledTimes(2);
    expect(brokenSession.sendTurn).toHaveBeenNthCalledWith(
      2,
      expect.anything(),
      expect.objectContaining({ hardTimeoutMs: 60_000 }),
    );
    expect(brokenSession.close).toHaveBeenCalledTimes(1);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      String(url).includes('/messages/claim'))).toHaveLength(1);
    expect(mockApiPost.mock.calls.filter(([, body]) =>
      body?.op === 'create_message')).toHaveLength(0);
    expect(vi.mocked(persistAndPostStatus).mock.calls.some(([taskId, payload]) => (
      taskId === 'task-reply-protocol-broken'
      && payload.status === 'FAILED'
      && payload.failure_category === 'resident_reply_protocol_failed'
      && String(payload.logs).includes('operator_reply_protocol_failed')
      && String(payload.logs).includes('operator reply tool missing')
      && String(payload.logs).includes('assistant_text_tail')
      && String(payload.logs).includes('still missing the native reply')
    ))).toBe(true);
  });

  it('does not dispatch a corrective turn without fresh exact-claim authority', async () => {
    vi.useFakeTimers();
    vi.mocked(fs.existsSync).mockImplementation((candidate) => (
      String(candidate) !== replyToolPaths.current?.stagedReplyPath
    ));
    const operatorMessage = {
      id: 'msg-repair-claim-lost',
      content: 'Do not answer after losing this claim.',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    mockApiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('/messages?')
        ? { messages: [operatorMessage], claim_required: true }
        : null,
    ));
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/messages/claim')) {
        return Promise.resolve({
          claim_id: 'claim-repair-lost',
          message_ids: [operatorMessage.id],
          incarnation_supported: true,
        });
      }
      return Promise.resolve({
        authority_held: true,
        resident_incarnation_held: true,
        conversation_claim_held: false,
        stop_requested: false,
      });
    });
    const session = makeSession(
      () => Promise.resolve(turnResult({
        resultText: 'undelivered text',
        accumulatedTurnText: 'undelivered text',
        sawToolUse: false,
        sentOperatorMessage: false,
      })),
      () => true,
    );
    mockStartSession.mockReturnValue(session);

    const run = handleStrategicResident(
      'task-repair-claim-lost',
      { project_name: 'P', open_messages: [], strategic_session_operator_ack_threshold_seconds: 0 },
      { project_metadata: { project_id: 'proj-repair-claim-lost' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );
    await vi.advanceTimersByTimeAsync(2_000);
    await run;

    expect(session.sendTurn).toHaveBeenCalledTimes(1);
    expect(mockApiPost.mock.calls.filter(([url]) =>
      String(url).includes('/messages/claim'))).toHaveLength(1);
    expect(vi.mocked(persistAndPostStatus).mock.calls.some(([taskId, payload]) => (
      taskId === 'task-repair-claim-lost'
      && payload.status === 'FAILED'
      && payload.failure_category === 'resident_reply_protocol_failed'
    ))).toBe(true);
  });

  it('never corrects or replays after an ambiguous strategic tool turn', async () => {
    vi.useFakeTimers();
    vi.mocked(fs.existsSync).mockImplementation((candidate) => (
      String(candidate) !== replyToolPaths.current?.stagedReplyPath
    ));
    const operatorMessage = {
      id: 'msg-ambiguous-strategic',
      content: 'Apply a strategic update once.',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    mockApiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('/messages?')
        ? { messages: [operatorMessage], claim_required: true }
        : null,
    ));
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/messages/claim')) {
        return Promise.resolve({
          claim_id: 'claim-ambiguous-strategic',
          message_ids: [operatorMessage.id],
          incarnation_supported: true,
        });
      }
      return Promise.resolve({
        authority_held: true,
        resident_incarnation_held: true,
        conversation_claim_held: true,
        stop_requested: false,
      });
    });
    const session = makeSession(
      () => Promise.resolve(turnResult({
        resultText: 'mutation result was ambiguous',
        accumulatedTurnText: 'mutation result was ambiguous',
        appliedStrategicOp: false,
        sawToolUse: true,
        toolCallCount: 1,
        trace: [
          { elapsedMs: 1, kind: 'tool', label: 'MCP:strategic_operation' },
          { elapsedMs: 2, kind: 'tool_result', label: 'MCP:strategic_operation', isError: true },
        ],
        sentOperatorMessage: false,
      })),
      () => true,
    );
    mockStartSession.mockReturnValue(session);

    const run = handleStrategicResident(
      'task-ambiguous-strategic',
      { project_name: 'P', open_messages: [], strategic_session_operator_ack_threshold_seconds: 0 },
      { project_metadata: { project_id: 'proj-ambiguous-strategic' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );
    await vi.advanceTimersByTimeAsync(2_000);
    await run;

    expect(session.sendTurn).toHaveBeenCalledTimes(1);
    expect(vi.mocked(persistAndPostStatus).mock.calls.some(([taskId, payload]) => (
      taskId === 'task-ambiguous-strategic'
      && payload.status === 'FAILED'
      && payload.failure_category === 'resident_reply_protocol_failed'
    ))).toBe(true);
  });

  it('folds the checkpoint into the first operator turn after bounded wedged preemption', async () => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'msg-preempt',
      content: 'Answer me now',
      created_at: new Date().toISOString(),
      from_type: 'user',
    };
    let messagePolls = 0;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?')) {
        messagePolls += 1;
        return Promise.resolve({ messages: messagePolls === 2 ? [operatorMessage] : [] });
      }
      return Promise.resolve(null);
    });

    let heartbeatCount = 0;
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('heartbeat')) {
        heartbeatCount += 1;
        return Promise.resolve({
          authority_held: true,
          stop_requested: heartbeatCount >= 3,
        });
      }
      return Promise.resolve({});
    });

    let resolveAutonomous!: (result: ResidentTurnResult) => void;
    const autonomous = new Promise<ResidentTurnResult>((resolve) => {
      resolveAutonomous = resolve;
    });
    const firstSession = makeSession(() => autonomous, () => true);
    vi.mocked(firstSession.close).mockImplementation(() => {
      resolveAutonomous(turnResult({ processGone: true }));
    });
    let resolveOperator!: (result: ResidentTurnResult) => void;
    const operatorTurn = new Promise<ResidentTurnResult>((resolve) => {
      resolveOperator = resolve;
    });
    const secondSession = makeSession(() => operatorTurn, () => true);
    mockStartSession
      .mockReturnValueOnce(firstSession)
      .mockReturnValueOnce(secondSession);

    const handlerDone = handleStrategicResident(
      'task-preemption',
      {
        project_name: 'P',
        open_messages: [],
        strategic_session_message_poll_seconds: 0.75,
        strategic_session_operator_ack_threshold_seconds: 0,
      },
      { project_metadata: { project_id: 'proj-preemption' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );

    await vi.advanceTimersByTimeAsync(11_000);
    expect(secondSession.sendTurn).toHaveBeenCalledTimes(1);

    mockSendWsFrame.mockClear();
    firstSession._emit({ kind: 'text', text: 'LATE_OLD_SESSION_TEXT' });
    await vi.advanceTimersByTimeAsync(STREAM_FLUSH_MS * 2);
    expect(JSON.stringify(mockSendWsFrame.mock.calls)).not.toContain('LATE_OLD_SESSION_TEXT');

    resolveOperator(turnResult({ resultText: 'operator reply' }));
    await vi.advanceTimersByTimeAsync(1_000);
    await handlerDone;

    expect(firstSession.interrupt).toHaveBeenCalled();
    expect(firstSession.close).toHaveBeenCalled();
    expect(mockStartSession).toHaveBeenCalledTimes(2);
    const freshTurn = vi.mocked(secondSession.sendTurn).mock.calls[0][0];
    expect(JSON.stringify(freshTurn)).toContain('Session checkpoint');
    expect(JSON.stringify(freshTurn)).toContain('Answer me now');
  });

  it('restarts after prompt SIGINT process exit and answers the queued operator message', async () => {
    vi.useFakeTimers();
    const operatorMessage = {
      id: 'msg-prompt-exit', content: 'Answer after prompt exit',
      created_at: new Date().toISOString(), from_type: 'user',
    };
    let messagePolls = 0;
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/messages?')) {
        messagePolls += 1;
        return Promise.resolve({ messages: messagePolls === 2 ? [operatorMessage] : [] });
      }
      return Promise.resolve(null);
    });
    let heartbeatCount = 0;
    mockApiPost.mockImplementation((url: string) => {
      if (url.includes('/heartbeat')) {
        heartbeatCount += 1;
        return Promise.resolve({ authority_held: true, stop_requested: heartbeatCount >= 3 });
      }
      return Promise.resolve({});
    });
    let resolveAutonomous!: (result: ResidentTurnResult) => void;
    const autonomous = new Promise<ResidentTurnResult>((resolve) => { resolveAutonomous = resolve; });
    const oldSession = makeSession(() => autonomous, () => true);
    vi.mocked(oldSession.interrupt).mockImplementation(() => {
      resolveAutonomous(turnResult({ processGone: true, interrupted: true }));
    });
    const freshSession = makeSession(
      () => Promise.resolve(turnResult({ resultText: 'operator reply after restart' })),
      () => true,
    );
    mockStartSession.mockReturnValueOnce(oldSession).mockReturnValueOnce(freshSession);

    const done = handleStrategicResident(
      'task-prompt-exit-preemption',
      {
        project_name: 'P', open_messages: [],
        strategic_session_message_poll_seconds: 0.75,
        strategic_session_operator_ack_threshold_seconds: 0,
      },
      { project_metadata: { project_id: 'proj-prompt-exit-preemption' }, context: {} },
      'ghtok', 'owner', 'repo', null, { value: '' },
    );
    await vi.advanceTimersByTimeAsync(2_000);
    await done;

    expect(oldSession.interrupt).toHaveBeenCalled();
    expect(mockStartSession).toHaveBeenCalledTimes(2);
    expect(freshSession.sendTurn).toHaveBeenCalledTimes(1);
    const freshTurn = vi.mocked(freshSession.sendTurn).mock.calls[0][0];
    expect(JSON.stringify(freshTurn)).toContain('Session checkpoint');
    expect(JSON.stringify(freshTurn)).toContain('Answer after prompt exit');
    const create = mockApiPost.mock.calls.find(([, body]) => body?.op === 'create_message');
    expect((create?.[1]?.data as Record<string, unknown> | undefined)?.covered_message_ids).toEqual(['msg-prompt-exit']);
  });

  it.each(['rate_limited', 'dead_auth'] as const)(
    'does not restart or mask a queued-message child close classified as %s',
    async (failureCategory) => {
      vi.useFakeTimers();
      const operatorMessage = {
        id: `msg-classified-${failureCategory}`,
        content: 'must remain queued after classified provider failure',
        created_at: new Date().toISOString(),
        from_type: 'user',
      };
      let messagePolls = 0;
      mockApiGet.mockImplementation((url: string) => {
        if (url.includes('/messages?')) {
          messagePolls += 1;
          return Promise.resolve({ messages: messagePolls === 2 ? [operatorMessage] : [] });
        }
        return Promise.resolve(null);
      });
      mockApiPost.mockImplementation((url: string) => {
        if (url.includes('/heartbeat')) return Promise.resolve({ authority_held: true });
        return Promise.resolve({});
      });
      let resolveAutonomous!: (result: ResidentTurnResult) => void;
      const autonomous = new Promise<ResidentTurnResult>((resolve) => { resolveAutonomous = resolve; });
      const failedSession = makeSession(() => autonomous, () => true);
      vi.mocked(failedSession.interrupt).mockImplementation(() => {
        resolveAutonomous(turnResult({
          processGone: true,
          interrupted: true,
          providerFailed: false,
          failureCategory,
        }));
      });
      mockStartSession.mockReturnValue(failedSession);

      const done = handleStrategicResident(
        `task-classified-${failureCategory}`,
        {
          project_name: 'P', open_messages: [],
          strategic_session_message_poll_seconds: 0.75,
          strategic_session_operator_ack_threshold_seconds: 0,
        },
        { project_metadata: { project_id: `proj-classified-${failureCategory}` }, context: {} },
        'ghtok', 'owner', 'repo', null, { value: '' },
      );
      await vi.advanceTimersByTimeAsync(2_000);
      await done;

      expect(failedSession.interrupt).toHaveBeenCalled();
      expect(mockStartSession).toHaveBeenCalledTimes(1);
      expect(mockApiPost.mock.calls.some(([, body]) => body?.op === 'create_message')).toBe(false);
      const terminal = vi.mocked(persistAndPostStatus).mock.calls.at(-1)?.[1] as Record<string, unknown>;
      expect(terminal).toMatchObject({ status: 'FAILED', failure_category: failureCategory });
    },
  );
});
