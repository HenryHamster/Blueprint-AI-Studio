import { createHash } from 'node:crypto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => {
  class RuntimeUnavailableError extends Error {
    readonly gracefulExitCode = 42;
  }
  return {
    apiGet: vi.fn(),
    apiPost: vi.fn(),
    sendLog: vi.fn(),
    runTextOnly: vi.fn(),
    RuntimeUnavailableError,
    mkdtempSync: vi.fn(() => '/tmp/bp-cold-successor-test'),
    chmodSync: vi.fn(),
    rmSync: vi.fn(),
  };
});

vi.mock('../index.js', () => ({
  apiGet: mocks.apiGet,
  apiPost: mocks.apiPost,
  sanitize: (value: string) => value,
  sendLog: mocks.sendLog,
}));

vi.mock('../providers/runners.js', () => ({
  RuntimeUnavailableError: mocks.RuntimeUnavailableError,
  runIsolatedTextOnlyMemory: mocks.runTextOnly,
}));

vi.mock('node:fs', () => ({
  default: {
    mkdtempSync: mocks.mkdtempSync,
    chmodSync: mocks.chmodSync,
    rmSync: mocks.rmSync,
  },
}));

import {
  classifyColdSuccessorClaim,
  handleStrategicColdSuccessor,
  preflightColdSuccessorClaim,
} from './strategic-cold-successor.js';

type JsonObject = Record<string, unknown>;
type ApiPostImplementation = (
  path: string,
  body: JsonObject,
  retries: number,
) => Promise<unknown>;
const COMPLETION_EFFECT_ID = '33333333-3333-4333-8333-333333333333';
let durableState: 'pending' | 'attested' | 'terminal';
let durableAttestation: JsonObject | null;
let durableTerminal: JsonObject | null;
let durableIdentity: JsonObject;

function durableReceipt(): JsonObject {
  return {
    schema_version: 1,
    receipt_state: durableState,
    session_id: durableIdentity.session_id,
    task_id: durableIdentity.task_id,
    worker_generation: durableIdentity.worker_generation,
    task_status: durableState === 'terminal' ? durableTerminal?.status : 'IN_PROGRESS',
    completion_terminal_effect_id: COMPLETION_EFFECT_ID,
    attestation: durableAttestation,
    terminal: durableTerminal,
  };
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value && typeof value === 'object') {
    const source = value as JsonObject;
    return `{${Object.keys(source).sort().map(key => (
      `${JSON.stringify(key)}:${canonicalJson(source[key])}`
    )).join(',')}}`;
  }
  const encoded = JSON.stringify(value);
  return encoded;
}

function sha256(value: string): string {
  return createHash('sha256').update(value, 'utf8').digest('hex');
}

function exactFixture(): { payload: JsonObject; task: JsonObject; semanticJson: string } {
  const projectId = '11111111-1111-4111-8111-111111111111';
  const problemModel = '{"checkpoint":"v57","unicode":"🐹"}';
  const runningUpdate = 'Release repair in progress.';
  const strategicMemory = '[]';
  const core = {
    schema_version: 1,
    project_id: projectId,
    safety: { paused: true, autonomous_enabled: false, archived: false },
    problem_model_canonical_json: problemModel,
    running_update: { present: true, text: runningUpdate },
    strategic_memory_canonical_json: strategicMemory,
  };
  const section = (encoding: string, value: string) => ({
    encoding,
    byte_count: Buffer.byteLength(value, 'utf8'),
    sha256: sha256(value),
  });
  const payload: JsonObject = {
    type: 'strategic_review',
    requires_browser: false,
    cold_successor_contract: {
      core,
      manifest: {
        problem_model: section('canonical-json', problemModel),
        running_update: section('utf-8', runningUpdate),
        strategic_memory: {
          ...section('canonical-json', strategicMemory),
          document_count: 0,
        },
        core: section('canonical-json', canonicalJson(core)),
      },
    },
  };
  return {
    payload,
    task: {
      task_id: '22222222-2222-4222-8222-222222222222',
      payloads: [payload],
      context: {
        _cold_successor: true,
        strategic_session_id: 'ss-cold-proof',
        _resident_worker_generation: 1,
      },
      project_metadata: { project_id: projectId },
      task_capability: { workspace_requirement: 'none' },
    },
    semanticJson: JSON.stringify({
      schema_version: 1,
      handoff_state: 'loaded',
      semantic_result: 'Loaded v57 durable authority.',
    }),
  };
}

function successfulProvider(semanticJson: string): JsonObject {
  return {
    exitCode: 0,
    allOutput: semanticJson,
    lastResultText: semanticJson,
    providerResultEventSeen: true,
    providerResultIsError: false,
    providerResultSubtype: 'success',
    providerTerminalReason: 'completed',
  };
}

function installSuccessfulApi(): void {
  durableState = 'pending';
  durableAttestation = null;
  durableTerminal = null;
  durableIdentity = {};
  mocks.apiGet.mockImplementation(() => Promise.resolve(durableReceipt()));
  mocks.apiPost.mockImplementation((path: string, body: JsonObject) => {
    if (path.endsWith('/heartbeat')) {
      durableIdentity = { ...body };
      return Promise.resolve({
        authority_held: true,
        resident_incarnation_held: true,
        stop_requested: false,
      });
    }
    if (path.endsWith('/attest')) {
      durableState = 'attested';
      durableAttestation = {
        schema_version: 1,
        ...body,
        completion_terminal_effect_id: COMPLETION_EFFECT_ID,
        attested_at: '2026-08-30T12:00:00Z',
      };
      return Promise.resolve({ status: 'attested' });
    }
    if (path.endsWith('/terminal')) {
      durableState = 'terminal';
      durableTerminal = {
        schema_version: 1,
        ...body,
        recorded_at: '2026-08-30T12:00:01Z',
      };
      return Promise.resolve({ status: 'accepted' });
    }
    throw new Error(`unexpected API path: ${path}`);
  });
}

async function runHandler(payload: JsonObject, task: JsonObject): Promise<void> {
  await handleStrategicColdSuccessor(
    '22222222-2222-4222-8222-222222222222',
    payload,
    task,
    '',
    '',
    '',
    null,
    { value: '' },
  );
}

describe('strategic cold successor', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installSuccessfulApi();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('fails closed on partial markers and a non-none workspace capability', () => {
    const { payload, task } = exactFixture();
    expect(classifyColdSuccessorClaim({}, {})).toBe('ordinary');
    expect(() => classifyColdSuccessorClaim(payload, { context: {} })).toThrow(
      'markers are incomplete or malformed',
    );
    expect(() => classifyColdSuccessorClaim({}, task)).toThrow(
      'markers are incomplete or malformed',
    );
    const wrongCapability = {
      ...task,
      task_capability: { workspace_requirement: 'repo' },
    };
    expect(() => preflightColdSuccessorClaim(payload, wrongCapability)).toThrow(
      'must deny workspace access',
    );
    expect(() => preflightColdSuccessorClaim(payload, {
      ...task,
      title: 'must not cross the cold claim boundary',
    })).toThrow('claim response has unexpected fields');
    expect(() => preflightColdSuccessorClaim(payload, {
      ...task,
      context: { ...(task.context as JsonObject), forbidden: true },
    })).toThrow('task context has unexpected fields');
    expect(() => preflightColdSuccessorClaim(payload, {
      ...task,
      payloads: [{ ...payload, forbidden: true }],
    })).toThrow('payload identity is inconsistent');
    expect(() => preflightColdSuccessorClaim(payload, {
      ...task,
      task_id: '',
    })).toThrow('task id must be non-empty text');
    expect(() => preflightColdSuccessorClaim(payload, {
      ...task,
      payloads: [],
    })).toThrow('must contain one exact payload');
    expect(() => preflightColdSuccessorClaim(payload, {
      ...task,
      project_metadata: {
        ...(task.project_metadata as JsonObject),
        project_name: 'forbidden',
      },
    })).toThrow('project metadata has unexpected fields');
    expect(() => preflightColdSuccessorClaim(payload, {
      ...task,
      task_capability: {
        workspace_requirement: 'none',
        agent_mode: 'read_only',
      },
    })).toThrow('task capability has unexpected fields');
    expect(mocks.apiPost).not.toHaveBeenCalled();
    expect(mocks.runTextOnly).not.toHaveBeenCalled();
    expect(mocks.mkdtempSync).not.toHaveBeenCalled();
  });

  it('attests and settles one dedicated successful terminal effect', async () => {
    const { payload, task, semanticJson } = exactFixture();
    mocks.runTextOnly.mockResolvedValue(successfulProvider(semanticJson));

    await runHandler(payload, task);

    const calls = mocks.apiPost.mock.calls as [string, JsonObject][];
    expect(calls.map(([path]) => path.split('/').at(-1))).toEqual([
      'heartbeat', 'attest', 'terminal',
    ]);
    expect(calls[1][1]).toMatchObject({
      provider_terminal_reason: 'completed',
      semantic_result: 'Loaded v57 durable authority.',
      provider_output_sha256: sha256(semanticJson),
    });
    expect(calls[2][1]).toMatchObject({
      status: 'COMPLETED',
      terminal_reason: 'completed',
      diagnostic_sha256: null,
    });
    expect(calls[0][1].resident_incarnation_id).toBe(calls[2][1].resident_incarnation_id);
    expect(mocks.apiPost.mock.calls.every(call => call[2] === 1)).toBe(true);
    expect(mocks.apiGet.mock.calls.every(call => call[1] === 1)).toBe(true);
    expect(task._cold_successor_terminal_status).toBe('COMPLETED');
  });

  it('resumes an exact attested incarnation without rerunning the provider', async () => {
    const { payload, task } = exactFixture();
    const successfulPost = mocks.apiPost.getMockImplementation() as
      ApiPostImplementation | undefined;
    if (!successfulPost) throw new Error('missing successful API fixture');
    mocks.apiPost.mockImplementation(async (path: string, body: JsonObject, retries: number) => {
      const response = await successfulPost(path, body, retries);
      if (path.endsWith('/heartbeat')) {
        const contract = payload.cold_successor_contract as JsonObject;
        const manifest = contract.manifest as JsonObject;
        const sectionHash = (name: string) => (manifest[name] as JsonObject).sha256;
        durableState = 'attested';
        durableAttestation = {
          schema_version: 1,
          ...body,
          core_sha256: sectionHash('core'),
          problem_model_sha256: sectionHash('problem_model'),
          running_update_sha256: sectionHash('running_update'),
          strategic_memory_sha256: sectionHash('strategic_memory'),
          provider_terminal_reason: 'completed',
          provider_output_sha256: 'a'.repeat(64),
          semantic_result: 'Loaded exact durable authority.',
          completion_terminal_effect_id: COMPLETION_EFFECT_ID,
          attested_at: '2026-08-30T12:00:00Z',
        };
      }
      return response;
    });
    mocks.runTextOnly.mockRejectedValue(new mocks.RuntimeUnavailableError('must not run'));

    await runHandler(payload, task);

    expect(mocks.runTextOnly).not.toHaveBeenCalled();
    expect((mocks.apiPost.mock.calls as [string, JsonObject][])
      .some(([path]) => path.endsWith('/attest'))).toBe(false);
    const terminal = (mocks.apiPost.mock.calls as [string, JsonObject][])
      .find(([path]) => path.endsWith('/terminal'))?.[1];
    expect(terminal?.terminal_effect_id).toBe(COMPLETION_EFFECT_ID);
    expect(task._cold_successor_terminal_status).toBe('COMPLETED');
  });

  it('reconciles a lost initial heartbeat response through the pure receipt', async () => {
    const { payload, task, semanticJson } = exactFixture();
    mocks.runTextOnly.mockResolvedValue(successfulProvider(semanticJson));
    const successfulPost = mocks.apiPost.getMockImplementation() as
      ApiPostImplementation | undefined;
    if (!successfulPost) throw new Error('missing successful API fixture');
    let firstHeartbeat = true;
    mocks.apiPost.mockImplementation(async (path: string, body: JsonObject, retries: number) => {
      const response = await successfulPost(path, body, retries);
      if (path.endsWith('/heartbeat') && firstHeartbeat) {
        firstHeartbeat = false;
        throw new Error('heartbeat response lost');
      }
      return response;
    });

    await runHandler(payload, task);

    expect(task._cold_successor_terminal_status).toBe('COMPLETED');
    expect(mocks.runTextOnly).toHaveBeenCalledOnce();
  });

  it('reconciles a lost attestation response without replaying provider work', async () => {
    const { payload, task, semanticJson } = exactFixture();
    mocks.runTextOnly.mockResolvedValue(successfulProvider(semanticJson));
    const successfulPost = mocks.apiPost.getMockImplementation() as
      ApiPostImplementation | undefined;
    if (!successfulPost) throw new Error('missing successful API fixture');
    let firstAttest = true;
    mocks.apiPost.mockImplementation(async (path: string, body: JsonObject, retries: number) => {
      const response = await successfulPost(path, body, retries);
      if (path.endsWith('/attest') && firstAttest) {
        firstAttest = false;
        throw new Error('attestation response lost');
      }
      return response;
    });

    await runHandler(payload, task);

    const attestCalls = (mocks.apiPost.mock.calls as [string, JsonObject][])
      .filter(([path]) => path.endsWith('/attest'));
    expect(attestCalls).toHaveLength(1);
    expect(mocks.runTextOnly).toHaveBeenCalledOnce();
    expect(task._cold_successor_terminal_status).toBe('COMPLETED');
  });

  it('terminalizes a definitively rejected attestation after an exact pending readback', async () => {
    const { payload, task, semanticJson } = exactFixture();
    mocks.runTextOnly.mockResolvedValue(successfulProvider(semanticJson));
    const successfulPost = mocks.apiPost.getMockImplementation() as
      ApiPostImplementation | undefined;
    if (!successfulPost) throw new Error('missing successful API fixture');
    mocks.apiPost.mockImplementation(async (path: string, body: JsonObject, retries: number) => {
      if (path.endsWith('/attest')) throw new Error('request failed (409): rejected');
      return await successfulPost(path, body, retries);
    });

    await runHandler(payload, task);

    const calls = mocks.apiPost.mock.calls as [string, JsonObject][];
    expect(calls.filter(([path]) => path.endsWith('/attest'))).toHaveLength(1);
    expect(calls.find(([path]) => path.endsWith('/terminal'))?.[1]).toMatchObject({
      status: 'FAILED',
      terminal_reason: 'attestation_failed',
    });
    expect(task._cold_successor_terminal_status).toBe('FAILED');
  });

  it('leaves a transport-ambiguous pending attestation for exact recovery', async () => {
    const { payload, task, semanticJson } = exactFixture();
    mocks.runTextOnly.mockResolvedValue(successfulProvider(semanticJson));
    const successfulPost = mocks.apiPost.getMockImplementation() as
      ApiPostImplementation | undefined;
    if (!successfulPost) throw new Error('missing successful API fixture');
    mocks.apiPost.mockImplementation(async (path: string, body: JsonObject, retries: number) => {
      if (path.endsWith('/attest')) throw new Error('ECONNRESET during attestation');
      return await successfulPost(path, body, retries);
    });

    await expect(runHandler(payload, task)).rejects.toThrow(
      'cold successor attestation delivery remains ambiguous',
    );

    const calls = mocks.apiPost.mock.calls as [string, JsonObject][];
    expect(calls.filter(([path]) => path.endsWith('/attest'))).toHaveLength(2);
    expect(calls.some(([path]) => path.endsWith('/terminal'))).toBe(false);
    expect(task._cold_successor_terminal_status).toBeUndefined();
  });

  it.each([
    ['nonzero exit', { exitCode: 1 }],
    ['missing result event', { providerResultEventSeen: undefined }],
    ['missing error predicate', { providerResultIsError: undefined }],
    ['provider error', { providerResultIsError: true }],
    ['wrong subtype', { providerResultSubtype: 'error' }],
    ['missing terminal reason', { providerTerminalReason: undefined }],
    ['api error reason', { providerTerminalReason: 'api_error' }],
    ['aborted streaming reason', { providerTerminalReason: 'aborted_streaming' }],
    ['unexpected api status', { providerApiErrorStatus: 200 }],
    ['api error status', { providerApiErrorStatus: 503 }],
  ])('rejects the provider terminal predicate: %s', async (_label, override) => {
    const { payload, task, semanticJson } = exactFixture();
    mocks.runTextOnly.mockResolvedValue({
      ...successfulProvider(semanticJson),
      ...override,
    });

    await runHandler(payload, task);

    const calls = mocks.apiPost.mock.calls as [string, JsonObject][];
    expect(calls.some(([path]) => path.endsWith('/attest'))).toBe(false);
    const terminal = calls.find(([path]) => path.endsWith('/terminal'));
    expect(terminal?.[1]).toMatchObject({
      status: 'FAILED',
      terminal_reason: 'provider_terminal_failed',
    });
    expect(task._cold_successor_terminal_status).toBe('FAILED');
  });

  it('uses a distinct failed effect for an invalid semantic result', async () => {
    const { payload, task } = exactFixture();
    mocks.runTextOnly.mockResolvedValue(successfulProvider('{"wrong":true}'));

    await runHandler(payload, task);

    const terminal = (mocks.apiPost.mock.calls as [string, JsonObject][])
      .find(([path]) => path.endsWith('/terminal'));
    expect(terminal?.[1]).toMatchObject({
      status: 'FAILED',
      terminal_reason: 'provider_result_invalid',
    });
  });

  it('classifies a provider execution rejection as terminal provider failure', async () => {
    const { payload, task } = exactFixture();
    mocks.runTextOnly.mockRejectedValue(new Error('provider execution timed out'));

    await runHandler(payload, task);

    const terminal = (mocks.apiPost.mock.calls as [string, JsonObject][])
      .find(([path]) => path.endsWith('/terminal'));
    expect(terminal?.[1]).toMatchObject({
      status: 'FAILED',
      terminal_reason: 'provider_terminal_failed',
    });
    expect(task._cold_successor_terminal_status).toBe('FAILED');
  });

  it('renews the exact lease during the provider run', async () => {
    vi.useFakeTimers();
    const { payload, task, semanticJson } = exactFixture();
    let resolveProvider!: (value: JsonObject) => void;
    mocks.runTextOnly.mockReturnValue(new Promise(resolve => {
      resolveProvider = resolve;
    }));

    const running = runHandler(payload, task);
    await vi.advanceTimersByTimeAsync(0);
    expect((mocks.apiPost.mock.calls as [string, JsonObject][])
      .filter(([path]) => path.endsWith('/heartbeat'))).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(45_000);
    const heartbeats = (mocks.apiPost.mock.calls as [string, JsonObject][])
      .filter(([path]) => path.endsWith('/heartbeat'));
    expect(heartbeats).toHaveLength(2);
    expect(heartbeats[1][1]).toEqual(heartbeats[0][1]);
    resolveProvider(successfulProvider(semanticJson));
    await running;
  });

  it('preserves runtime-unavailable requeue semantics without terminalizing', async () => {
    const { payload, task } = exactFixture();
    const unavailable = new mocks.RuntimeUnavailableError('no verified runtime');
    mocks.runTextOnly.mockRejectedValue(unavailable);

    await expect(runHandler(payload, task)).rejects.toBe(unavailable);

    const paths = (mocks.apiPost.mock.calls as [string, JsonObject][])
      .map(([path]) => path);
    expect(paths.some(path => path.endsWith('/attest'))).toBe(false);
    expect(paths.some(path => path.endsWith('/terminal'))).toBe(false);
    expect(task._cold_successor_terminal_status).toBeUndefined();
  });

  it('reconciles an ambiguous successful terminal delivery without FAILED', async () => {
    const { payload, task, semanticJson } = exactFixture();
    mocks.runTextOnly.mockResolvedValue(successfulProvider(semanticJson));
    mocks.apiPost.mockImplementation((path: string, body: JsonObject) => {
      if (path.endsWith('/heartbeat')) {
        durableIdentity = { ...body };
        return Promise.resolve({
          authority_held: true,
          resident_incarnation_held: true,
          stop_requested: false,
        });
      }
      if (path.endsWith('/attest')) {
        durableState = 'attested';
        durableAttestation = {
          schema_version: 1,
          ...body,
          completion_terminal_effect_id: COMPLETION_EFFECT_ID,
          attested_at: '2026-08-30T12:00:00Z',
        };
        return Promise.resolve({ status: 'attested' });
      }
      if (path.endsWith('/terminal')) {
        durableState = 'terminal';
        durableTerminal = {
          schema_version: 1,
          ...body,
          recorded_at: '2026-08-30T12:00:01Z',
        };
        throw new Error('response lost');
      }
      throw new Error(`unexpected API path: ${path}`);
    });

    await runHandler(payload, task);

    const terminalBodies = (mocks.apiPost.mock.calls as [string, JsonObject][])
      .filter(([path]) => path.endsWith('/terminal'))
      .map(([, body]) => body);
    expect(terminalBodies).toHaveLength(1);
    expect(terminalBodies[0].status).toBe('COMPLETED');
    expect(task._cold_successor_terminal_status).toBe('COMPLETED');
  });

  it('recovers durable effects across ambiguous responses and new incarnations', async () => {
    const first = exactFixture();
    const second = exactFixture();
    const third = exactFixture();
    mocks.runTextOnly.mockResolvedValue(successfulProvider(first.semanticJson));
    let loseNextReceiptRead = false;
    const attestBodies: JsonObject[] = [];
    const terminalBodies: JsonObject[] = [];

    mocks.apiGet.mockImplementation(() => {
      if (loseNextReceiptRead) {
        loseNextReceiptRead = false;
        return Promise.reject(new Error('durable receipt response lost'));
      }
      return Promise.resolve(durableReceipt());
    });
    mocks.apiPost.mockImplementation((path: string, body: JsonObject) => {
      if (path.endsWith('/heartbeat')) {
        durableIdentity = { ...body };
        return Promise.resolve({
          authority_held: true,
          resident_incarnation_held: true,
          stop_requested: false,
        });
      }
      if (path.endsWith('/attest')) {
        attestBodies.push({ ...body });
        durableState = 'attested';
        durableAttestation = {
          schema_version: 1,
          ...body,
          completion_terminal_effect_id: COMPLETION_EFFECT_ID,
          attested_at: '2026-08-30T12:00:00Z',
        };
        loseNextReceiptRead = true;
        throw new Error('attestation response lost');
      }
      if (path.endsWith('/terminal')) {
        terminalBodies.push({ ...body });
        durableState = 'terminal';
        durableTerminal = {
          schema_version: 1,
          ...body,
          recorded_at: '2026-08-30T12:00:01Z',
        };
        loseNextReceiptRead = true;
        throw new Error('terminal response lost');
      }
      throw new Error(`unexpected API path: ${path}`);
    });

    await expect(runHandler(first.payload, first.task)).rejects.toThrow(
      'attestation receipt read is ambiguous',
    );
    expect(first.task._cold_successor_terminal_status).toBeUndefined();
    await expect(runHandler(second.payload, second.task)).rejects.toThrow(
      'durable receipt response lost',
    );
    expect(second.task._cold_successor_terminal_status).toBeUndefined();
    await runHandler(third.payload, third.task);

    expect(mocks.runTextOnly).toHaveBeenCalledOnce();
    expect(attestBodies).toHaveLength(1);
    expect(terminalBodies).toHaveLength(1);
    expect(terminalBodies[0]).toMatchObject({
      status: 'COMPLETED',
      terminal_reason: 'completed',
      diagnostic_sha256: null,
    });
    expect(terminalBodies.some(body => body.status === 'FAILED')).toBe(false);
    expect(attestBodies[0].resident_incarnation_id).not.toBe(
      terminalBodies[0].resident_incarnation_id,
    );
    expect(third.task._cold_successor_terminal_status).toBe('COMPLETED');
  });
});
