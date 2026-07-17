import { vi, describe, it, expect, beforeEach } from 'vitest';

const residentTurnHandlerMock = vi.hoisted(() => vi.fn().mockResolvedValue(undefined));

// ── Module mocks ──────────────────────────────────────────────────────────────
// Mock ../index.js (the main agent runner) to prevent main() from executing
// and to supply the named exports that handler files import at module level.
vi.mock('../index.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../index.js')>();
  return {
    ...actual,
    WORKSPACE: '/tmp/workspace',
    sendLog: vi.fn(),
    log: vi.fn(),
    getAgentToken: vi.fn().mockReturnValue(''),
    apiPost: vi.fn(),
    apiGet: vi.fn(),
    persistAndPostStatus: vi.fn(),
    readHandlerOutput: vi.fn(),
    validateBranchName: vi.fn((b: string, _label: string) => b),
    git: vi.fn().mockReturnValue(''),
    setupWorkspace: vi.fn(),
    pushAndCreatePR: vi.fn(),
    runClaudeCode: vi.fn(),
    testFixLoop: vi.fn(),
    cloneWithRetry: vi.fn(),
    sendHeartbeatSync: vi.fn(),
  };
});

vi.mock('../workspacePrep.js', () => ({
  prepareWorkspaceForRole: vi.fn().mockResolvedValue(undefined),
}));

// handlers/index.ts imports the three audit handlers from ./audits.js.
// Mock them here so the registry tests are isolated from the audit handler
// implementation; the implementations are exercised in audits.test.ts.
vi.mock('./audits.js', () => ({
  handleSecurityAudit: vi.fn().mockResolvedValue(undefined),
  handleSelfAudit: vi.fn().mockResolvedValue(undefined),
  handleReleaseAudit: vi.fn().mockResolvedValue(undefined),
}));

// handleReleaseAuditChallenge lives in audit-challenge.ts (not audits.ts).
// Mock it separately so the registry test can compare function references
// and confirm there is exactly one registration under release_audit_challenge.
vi.mock('./audit-challenge.js', () => ({
  handleReleaseAuditChallenge: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('./strategic-resident-turn.js', () => ({
  handleStrategicResidentTurn: residentTurnHandlerMock,
}));

// Mock fs and child_process so handler files that import them at module level
// don't cause side effects.
vi.mock('fs', () => ({
  default: {
    readFileSync: vi.fn().mockReturnValue(''),
    writeFileSync: vi.fn(),
    existsSync: vi.fn().mockReturnValue(false),
    appendFileSync: vi.fn(),
    mkdirSync: vi.fn(),
    readdirSync: vi.fn().mockReturnValue([]),
    unlinkSync: vi.fn(),
  },
}));

vi.mock('child_process', () => ({
  execFileSync: vi.fn().mockReturnValue(Buffer.from('')),
  execFile: vi.fn(),
  spawn: vi.fn().mockReturnValue({
    stderr: { on: vi.fn() },
    stdout: { on: vi.fn() },
    on: vi.fn(),
    kill: vi.fn(),
  }),
}));

// ws is imported as "type WebSocket" (erased at runtime) in handler files, but
// the underlying module may still be evaluated. Provide a minimal mock.
vi.mock('ws', () => ({
  default: class MockWebSocket {
    static OPEN = 1;
    readyState = 0;
    on = vi.fn();
    send = vi.fn();
    close = vi.fn();
  },
}));

// ── Import under test ─────────────────────────────────────────────────────────
import { ALL_AGENT_TASK_TYPES } from '../taskCapabilities.js';
import type { AgentTaskType } from '../taskCapabilities.js';
import {
  handlers,
  resolveHandler,
  resolveGenericRunner,
  resolveTaskCapabilityContract,
  type TaskCapabilityContract,
} from './index.js';
import { DISPATCHER_MEMO_SYNTHESIZER_KINDS } from './memo.js';
import { persistAndPostStatus } from '../index.js';
import { expectCalledWith } from '../utils/typed-mock-call.js';
import { handleReleaseAuditChallenge } from './audit-challenge.js';

// ── Handler registry tests ────────────────────────────────────────────────────
describe('handlers registry', () => {
  const EXPECTED_TYPES = ALL_AGENT_TASK_TYPES;

  // 27 BESPOKE handlers are hand-registered today. This is no longer the
  // launchability gate — dispatch falls back to contract-driven GENERIC runners
  // for registered types with no bespoke handler (see the contract-driven
  // dispatch suite below). The count still documents the bespoke set so a
  // dropped/duplicated bespoke entry is caught.
  it('has exactly the registered bespoke handler entries', () => {
    expect(Object.keys(handlers)).toHaveLength(ALL_AGENT_TASK_TYPES.length);
  });

  it.each(EXPECTED_TYPES)('contains bespoke handler for task type "%s"', (taskType) => {
    expect(handlers).toHaveProperty(taskType);
  });

  // The launchability invariant, restated for contract-driven dispatch: every
  // type in the shared capability table must resolve to SOME runner (bespoke or
  // generic) — NOT necessarily a bespoke handler.
  it.each(EXPECTED_TYPES)('every registered type "%s" resolves to a runner (bespoke or generic)', (taskType) => {
    const resolution = resolveHandler(taskType, {}, { type: taskType });
    expect(resolution.handler, `"${taskType}" must resolve to a runner`).toBeTypeOf('function');
  });

  it('every registered handler is a function', () => {
    for (const [type, handler] of Object.entries(handlers)) {
      expect(typeof handler, `handler for "${type}" should be a function`).toBe('function');
    }
  });

  it('no handler value is null or undefined', () => {
    for (const [type, handler] of Object.entries(handlers)) {
      expect(handler, `handler for "${type}" should not be null/undefined`).toBeDefined();
      expect(handler, `handler for "${type}" should not be null`).not.toBeNull();
    }
  });

  it('strategic_review and research are distinct handlers — research dispatches to strategic only when payload.research_mode is "strategic"', () => {
    expect(handlers.strategic_review).not.toBe(handlers.research);
  });

  it('release_audit_challenge maps to handleReleaseAuditChallenge — single registration, no dead divergent copy', () => {
    // Confirm the registry uses the audit-challenge.ts export, not any stale copy
    // that might exist in audits.ts. The mocked reference must match exactly.
    expect(handlers.release_audit_challenge).toBe(handleReleaseAuditChallenge);
    const resolution = resolveHandler('release_audit_challenge', {}, { type: 'release_audit_challenge' });
    expect(resolution.dispatch).toBe('bespoke');
    expect(resolution.handler).toBe(handleReleaseAuditChallenge);
  });

  it('passes the server-projected Resident provider fence from payloads[0] to its handler', async () => {
    const claimTask = {
      payloads: [{
        type: 'strategic_resident_turn',
        _resident_expected_runtime_provider: 'anthropic',
      }],
    };
    const payload = claimTask.payloads[0];
    const resolution = resolveHandler(payload.type, claimTask, payload);
    expect(resolution.dispatch).toBe('bespoke');
    expect(resolution.handler).not.toBeNull();
    if (resolution.handler === null) throw new Error('Resident handler missing');

    await resolution.handler(
      'task-id', payload, claimTask, '', '', '', null, { value: '' },
    );
    expect(residentTurnHandlerMock).toHaveBeenCalledWith(
      'task-id',
      expect.objectContaining({ _resident_expected_runtime_provider: 'anthropic' }),
      claimTask,
      '', '', '', null, { value: '' },
    );
  });

  it('all handlers are distinct function references from each other', () => {
    const seen = new Set<unknown>();
    for (const type of EXPECTED_TYPES) {
      const fn = handlers[type];
      expect(seen.has(fn), `duplicate handler found for "${type}"`).toBe(false);
      seen.add(fn);
    }
  });
});

// ── Contract-driven generic dispatch ──────────────────────────────────────────
// A task type the backend REGISTERS but that the agent has no bespoke handler for
// must still be runnable: it routes to a GENERIC runner selected from the
// published `task_capability` contract (run_shape × agent_mode × output_kind),
// instead of throwing "Unknown task type". The generic runners reuse existing
// machinery exactly — read-only knowledge → handleInvestigate (handlers.investigate),
// write/PR → handleCreate (handlers.create) — so the declared output is persisted
// and graded by the type's evaluator with NO bespoke completion handler required.
describe('contract-driven generic dispatch', () => {
  const knowledgeContract: TaskCapabilityContract = {
    id: 'research',
    label: 'Research',
    description: 'Analyze the codebase and produce findings.',
    default_category: 'strategic',
    user_launchable: true,
    provider_configurable: true,
    filter_visible: true,
    run_shape: 'single_agent',
    agent_mode: 'read_only',
    output_kind: 'knowledge',
    requires_code: true,
    requires_web: false,
    requires_compute: false,
    evaluation_strategy: 'forward_quality',
    cost_ceiling_usd: 4,
    is_bespoke_dispatch: false,
  };
  // The backend emits run_shape='workflow' for every PR-producing write type
  // (create, fix, qa, deps, docs, migrate, refactor) — see RunShape in
  // backend/app/task_capabilities.py. The generic write/PR runner keys off that
  // exact shape, so this fixture mirrors what the backend actually publishes.
  const writeContract: TaskCapabilityContract = {
    id: 'create',
    label: 'Create',
    description: 'Implement new functionality and open a PR.',
    default_category: 'build',
    user_launchable: true,
    provider_configurable: true,
    filter_visible: true,
    run_shape: 'workflow',
    agent_mode: 'write',
    output_kind: 'pr',
    requires_code: true,
    requires_web: false,
    requires_compute: false,
    evaluation_strategy: 'ci_and_review',
    cost_ceiling_usd: 8,
    is_bespoke_dispatch: false,
  };

  it('routes an UNKNOWN read-only knowledge type to the generic investigate runner (no throw)', () => {
    const payload = { type: 'knowledge_probe', prompt: 'investigate X' };
    const task = { task_capability: knowledgeContract };
    const resolution = resolveHandler('knowledge_probe', task, payload);
    expect(resolution.dispatch).toBe('generic');
    // Generic read-only knowledge reuses the SAME machinery as `investigate`,
    // so it persists its output via the status/artifact path identically.
    expect(resolution.handler).toBe(handlers.investigate);
    if (resolution.dispatch === 'generic') {
      expect(resolution.runnerLabel).toContain('read-only knowledge');
    }
  });

  it('routes an UNKNOWN write/PR type to the generic create runner (no throw)', () => {
    const payload = { type: 'scaffold_feature', prompt: 'build Y' };
    const task = { task_capability: writeContract };
    const resolution = resolveHandler('scaffold_feature', task, payload);
    expect(resolution.dispatch).toBe('generic');
    // Generic write/PR reuses the SAME machinery as `create`, so it pushes a
    // branch and opens a PR identically — honored by the backend PR pipeline.
    expect(resolution.handler).toBe(handlers.create);
    if (resolution.dispatch === 'generic') {
      expect(resolution.runnerLabel).toContain('write/PR');
    }
  });

  it('accepts the hyphenated run-shape spelling ("single-agent")', () => {
    const payload = { type: 'knowledge_probe', prompt: 'investigate X' };
    const task = { task_capability: { ...knowledgeContract, run_shape: 'single-agent' } };
    const resolution = resolveHandler('knowledge_probe', task, payload);
    expect(resolution.dispatch).toBe('generic');
    expect(resolution.handler).toBe(handlers.investigate);
  });

  it('preserves extra backend-published catalog fields while normalizing dispatch keys', () => {
    const payload = { type: 'research', prompt: 'findings' };
    const task = {
      task_capability: {
        ...knowledgeContract,
        run_shape: 'single-agent',
        description: 'Analyze the codebase and produce findings.',
      },
    };
    const contract = resolveTaskCapabilityContract(task, payload);

    expect(contract).toMatchObject({
      id: 'research',
      label: 'Research',
      description: 'Analyze the codebase and produce findings.',
      default_category: 'strategic',
      user_launchable: true,
      provider_configurable: true,
      filter_visible: true,
      agent_mode: 'read_only',
      output_kind: 'knowledge',
      requires_code: true,
      requires_web: false,
      requires_compute: false,
      evaluation_strategy: 'forward_quality',
      cost_ceiling_usd: 4,
      run_shape: 'single_agent',
      is_bespoke_dispatch: false,
    });
  });

  it('treats output_kind=none as generic knowledge too', () => {
    const task = { task_capability: { ...knowledgeContract, output_kind: 'none' } };
    const resolution = resolveHandler('probe_none', task, { type: 'probe_none', prompt: 'x' });
    expect(resolution.handler, 'output_kind=none should route to generic knowledge').toBe(handlers.investigate);
  });

  it('rejects_generic_read_only_verdict_without_explicit_runner', () => {
    const task = { task_capability: { ...knowledgeContract, output_kind: 'verdict' } };
    const resolution = resolveHandler('merge_test_verdict_probe', task, {
      type: 'merge_test_verdict_probe',
      prompt: 'produce a real merge-test verdict',
    });
    expect(resolution.handler).toBeNull();
    if (resolution.handler === null) {
      expect(resolution.dispatch).toBe('none');
      expect(resolution.reason).toContain('merge_test_verdict_probe');
      expect(resolution.reason).toContain('output_kind=verdict');
      expect(resolution.reason).toContain('Generic dispatch supports single_agent read_only→knowledge/none');
    }
  });

  it('a BESPOKE handler still wins over the contract (precedence preserved)', () => {
    // create has a bespoke handler; even with a read-only contract present, the
    // bespoke handler must be chosen so existing behavior is preserved exactly.
    const task = { task_capability: knowledgeContract };
    const resolution = resolveHandler('create', task, { type: 'create', prompt: 'x' });
    expect(resolution.dispatch).toBe('bespoke');
    expect(resolution.handler).toBe(handlers.create);
  });

  it('a BESPOKE verdict handler still wins over a verdict contract', () => {
    const task = { task_capability: { ...knowledgeContract, id: 'verify_tests', output_kind: 'verdict' } };
    const resolution = resolveHandler('verify_tests', task, {
      type: 'verify_tests',
      prompt: 'produce a real merge-test verdict',
    });
    expect(resolution.dispatch).toBe('bespoke');
    expect(resolution.handler).toBe(handlers.verify_tests);
  });

  it('fails LOUDLY (clear message, no silent wrong work) for a run-shape with no generic runner yet', () => {
    // Use a type with NO bespoke handler so dispatch falls through to the
    // contract-driven generic path (every registered type has a bespoke
    // handler that would win first — see the precedence test above).
    for (const run_shape of ['multi_agent', 'experiment_loop']) {
      const task = { task_capability: { ...writeContract, run_shape } };
      const resolution = resolveHandler('unmapped_shape_type', task, { type: 'unmapped_shape_type', prompt: 'x' });
      expect(resolution.handler, `${run_shape} must not resolve to a runner yet`).toBeNull();
      if (resolution.handler === null) {
        expect(resolution.reason).toContain(run_shape);
        expect(resolution.reason).toContain('unmapped_shape_type');
      }
    }
  });

  it('fails loudly for an unsupported single-agent output_kind (e.g. write→artifact)', () => {
    const task = { task_capability: { ...writeContract, output_kind: 'artifact' } };
    const resolution = resolveHandler('artifact_builder', task, { type: 'artifact_builder', prompt: 'x' });
    expect(resolution.handler).toBeNull();
  });

  describe('contract ABSENT — fallback to local table + payload', () => {
    it('derives read_only/knowledge for a locally-known read-only type', () => {
      const contract = resolveTaskCapabilityContract({}, { type: 'investigate' });
      expect(contract.agent_mode).toBe('read_only');
      expect(contract.output_kind).toBe('knowledge');
      expect(contract.run_shape).toBe('single_agent');
    });

    it('derives workflow write/pr for a type unknown to the local table', () => {
      const contract = resolveTaskCapabilityContract({}, { type: 'brand_new_type' });
      expect(contract.agent_mode).toBe('write');
      expect(contract.output_kind).toBe('pr');
      // Mirrors the backend run_shape for write/PR types so the derived contract
      // routes through the same generic workflow runner a published contract would.
      expect(contract.run_shape).toBe('workflow');
    });

    it('an unknown write type with NO contract still resolves to the generic create runner', () => {
      const resolution = resolveHandler('brand_new_type', {}, { type: 'brand_new_type', prompt: 'x' });
      expect(resolution.dispatch).toBe('generic');
      expect(resolution.handler).toBe(handlers.create);
    });
  });

  describe('resolveGenericRunner unit', () => {
    it('returns null for reserved/unknown run shapes (experiment_loop)', () => {
      expect(resolveGenericRunner({ ...writeContract, run_shape: 'experiment_loop' })).toBeNull();
    });

    it('returns the investigate handler for single-agent read-only knowledge', () => {
      const runner = resolveGenericRunner(knowledgeContract);
      expect(runner?.handler).toBe(handlers.investigate);
    });

    it('returns the create handler for workflow write/pr', () => {
      const runner = resolveGenericRunner(writeContract);
      expect(runner?.handler).toBe(handlers.create);
    });
  });
});

// ── Task class classification (cross-cutting structural invariants) ───────────
// Maps every registered task type to its prompt-safety class. When a new type
// is added to handlers/index.ts, these tests fail until the owner declares which
// class it belongs to — preventing silent drift in cross-cutting guidance blocks.
//
// Class contracts (enforced per-handler in sibling test files):
//   code-change  — MUST include CODE_CHANGE_HELPER_GUIDANCE + assembleFailureGuidanceBlocks()
//                  + PLAYBOOK_GUIDANCE + DEFAULT_TOOL_GUIDANCE
//   read-only    — MUST NOT include CODE_CHANGE_HELPER_GUIDANCE (no code push)
//                  MUST include PLAYBOOK_GUIDANCE + DEFAULT_TOOL_GUIDANCE
//   research     — MUST include PLAYBOOK_GUIDANCE + DEFAULT_TOOL_GUIDANCE
//                  MUST NOT include CODE_CHANGE_HELPER_GUIDANCE

type TaskClass = 'code-change' | 'read-only' | 'research';

const TASK_CLASS_MAP = {
  // Code-change (7): ship code via ./check → ./pr open → ./done
  create:            'code-change',
  fix:               'code-change',
  refactor:          'code-change',
  migrate:           'code-change',
  docs:              'code-change',
  qa:                'code-change',
  deps:              'code-change',
  // Salvage bundle (1): consolidated fix PR from decayed research findings
  salvage_bundle:    'code-change',
  // Read-only / investigative (14): analysis only, must not push code
  review:            'read-only',
  panel_review:      'read-only',
  security_audit:    'read-only',
  self_audit:        'read-only',
  release_audit:     'read-only',
  release_audit_challenge: 'read-only',
  release_merge_grade: 'read-only',
  smoketest:         'read-only',
  debug:             'read-only',
  perf:              'read-only',
  investigate:       'read-only',
  verify_tests:      'read-only',
  compute_experiment:'read-only',
  repo_bootstrap:    'read-only',
  capability_proof_trial: 'read-only',
  strategic_resident_turn: 'read-only',
  // Research / planning (7): read-heavy, no code shipped
  plan:              'research',
  research:          'research',
  strategic_review:  'research',
  design_doc_update: 'research',
  memo:              'research',
  web_research:      'research',
  design:            'research',
} as const satisfies Record<AgentTaskType, TaskClass>;

describe('task type class classification — cross-cutting invariants', () => {
  it('every registered task type has an explicit class assignment', () => {
    for (const type of Object.keys(handlers) as AgentTaskType[]) {
      expect(TASK_CLASS_MAP, `"${type}" is missing from TASK_CLASS_MAP — add it to enforce cross-cutting prompt invariants`).toHaveProperty(type);
    }
  });

  it('class map has no phantom entries absent from the handlers registry', () => {
    const registered = new Set(Object.keys(handlers));
    for (const type of Object.keys(TASK_CLASS_MAP)) {
      expect(registered, `"${type}" is in TASK_CLASS_MAP but not in the handlers registry`).toContain(type);
    }
  });

  it('class distribution is 8 code-change + 16 read-only + 7 research = 31 total', () => {
    const counts: Record<TaskClass, number> = { 'code-change': 0, 'read-only': 0, research: 0 };
    for (const cls of Object.values(TASK_CLASS_MAP)) counts[cls]++;
    expect(counts['code-change']).toBe(8);
    expect(counts['read-only']).toBe(16);
    expect(counts['research']).toBe(7);
  });

  it('no task type belongs to more than one class', () => {
    const seen = new Set<string>();
    for (const type of Object.keys(TASK_CLASS_MAP)) {
      expect(seen, `"${type}" appears more than once in TASK_CLASS_MAP`).not.toContain(type);
      seen.add(type);
    }
  });
});

// ── Strategic dispatch smoke ──────────────────────────────────────────────────
// Locks in the contract that handlers.research forwards strategic-mode payloads
// to handleStrategic, and that handlers.strategic_review goes directly to it.
// Both paths must write the strategic helper script — that's the observable
// signal that the strategic flow ran (not the tactical research flow).
describe('strategic dispatch smoke', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('handlers.strategic_review writes the strategic helper to the workspace', async () => {
    const { runClaudeCode } = await import('../index.js');
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    const fs = (await import('fs')).default;
    vi.mocked(fs.existsSync as (p: string) => boolean).mockReturnValue(false);

    const payload = { base_branch: 'main', research_mode: 'strategic' };
    const task = { title: 'Strategic', description: '', context: {} };
    await handlers.strategic_review('smoke-strategic-001', payload, task, '', 'owner', 'repo', null, { value: '' });

    const writeCalls = vi.mocked(fs.writeFileSync).mock.calls as unknown as [string, string][];
    const strategicHelperWritten = writeCalls.some(([path]) => path.endsWith('/strategic'));
    expect(strategicHelperWritten, 'strategic helper script must be written by strategic_review dispatch').toBe(true);
    const fetchHelperWritten = writeCalls.some(([path]) => path.endsWith('/fetch'));
    expect(fetchHelperWritten, 'fetch helper script must be written by strategic_review dispatch').toBe(true);
  });

  it('handlers.research with research_mode="strategic" routes to handleStrategic (writes strategic helper)', async () => {
    const { runClaudeCode } = await import('../index.js');
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    const fs = (await import('fs')).default;
    vi.mocked(fs.existsSync as (p: string) => boolean).mockReturnValue(false);

    const payload = { base_branch: 'main', research_mode: 'strategic' };
    const task = { title: 'Strategic', description: '', context: {} };
    await handlers.research('smoke-research-strategic-001', payload, task, '', 'owner', 'repo', null, { value: '' });

    const writeCalls = vi.mocked(fs.writeFileSync).mock.calls as unknown as [string, string][];
    const strategicHelperWritten = writeCalls.some(([path]) => path.endsWith('/strategic'));
    expect(strategicHelperWritten, 'research dispatch must reach handleStrategic and write the strategic helper').toBe(true);
  });
});

// ── Memo bake-smoke dispatcher fixture kinds guard ────────────────────────────
// Fails fast if any bake-smoke dispatcher fixture exercises a reasoning_kind
// the synthesizer no longer handles credential-free — catching the regression
// at unit-test time instead of at Packer image build time.
//
// To add a new bake-smoke dispatcher fixture, update BAKE_SMOKE_DISPATCHER_FIXTURES
// here AND add the kind to DISPATCHER_MEMO_SYNTHESIZER_KINDS in memo.ts.
describe('memo bake-smoke dispatcher fixture kinds guard', () => {
  // One entry per bake-smoke file that uses the dispatcher synthesis path
  // (source_finding present + reasoning_kind the synthesizer handles).
  const BAKE_SMOKE_DISPATCHER_FIXTURES: Array<{ file: string; kind: string }> = [
    // memo-bake-smoke.ts: memoTaskPayload.reasoning_kind
    { file: 'memo-bake-smoke.ts', kind: 'direction_retirement' },
    // memo-bake-smoke-lifecycle.ts: MEMO_TASK.payloads[0].reasoning_kind
    { file: 'memo-bake-smoke-lifecycle.ts', kind: 'direction_retirement' },
  ];

  it.each(BAKE_SMOKE_DISPATCHER_FIXTURES)(
    'bake-smoke dispatcher fixture kind "$kind" (from $file) is handled by the synthesizer',
    ({ file, kind }) => {
      expect(
        DISPATCHER_MEMO_SYNTHESIZER_KINDS as readonly string[],
        `bake-smoke fixture in ${file} uses reasoning_kind "${kind}" which is absent from ` +
        `DISPATCHER_MEMO_SYNTHESIZER_KINDS — the synthesizer will not handle it credential-free ` +
        `and the agent image bake will fail`,
      ).toContain(kind);
    },
  );
});

// ── Memo bake smoke contract ──────────────────────────────────────────────────
// Locks in the contract exercised by the image bake: handlers.memo must resolve
// with COMPLETED when invoked through the registry with a credential-free
// dispatcher payload. Prevents future packer edits from silently dropping the
// memo smoke step and shipping a broken handler in the baked image.
describe('memo bake smoke contract', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('handlers.memo is a callable function accessible through the registry', () => {
    expect(typeof handlers.memo).toBe('function');
  });

  it('handlers.memo completes with COMPLETED for a direction_retirement dispatcher payload (no Claude call, credential-free)', async () => {
    const payload = {
      reasoning_kind: 'direction_retirement',
      source_task_id: 'task-bake-verify',
      direction_id: 'direction-bake-verify',
      source_finding: {
        rationale: 'Direction completed its goals and is retired.',
        direction_text: 'CPU spike investigation direction',
      },
    };
    const task = { title: 'Bake smoke memo', description: 'Memo smoke', context: {} };

    await handlers.memo('bake-smoke-001', payload, task, '', 'owner', 'repo', null, { value: '' });

    expectCalledWith(persistAndPostStatus,
      'bake-smoke-001',
      expect.objectContaining({
        status: 'COMPLETED',
        logs: expect.stringContaining('Direction completed its goals and is retired.') as string,
      }),
    );
    // runClaudeCode must NOT be called — dispatcher path synthesizes directly
    const { runClaudeCode } = await import('../index.js');
    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
  });
});
