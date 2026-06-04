import { vi, describe, it, expect, beforeEach } from 'vitest';

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
import { handlers } from './index.js';
import { persistAndPostStatus } from '../index.js';
import { expectCalledWith } from '../utils/typed-mock-call.js';

// ── Handler registry tests ────────────────────────────────────────────────────
describe('handlers registry', () => {
  const EXPECTED_TYPES = ALL_AGENT_TASK_TYPES;

  it('has exactly 21 handler entries', () => {
    expect(Object.keys(handlers)).toHaveLength(21);
  });

  it.each(EXPECTED_TYPES)('contains handler for task type "%s"', (taskType) => {
    expect(handlers).toHaveProperty(taskType);
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

  it('all handlers are distinct function references from each other', () => {
    const seen = new Set<unknown>();
    for (const type of EXPECTED_TYPES) {
      const fn = handlers[type];
      expect(seen.has(fn), `duplicate handler found for "${type}"`).toBe(false);
      seen.add(fn);
    }
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
  // Read-only / investigative (9): analysis only, must not push code
  review:            'read-only',
  security_audit:    'read-only',
  self_audit:        'read-only',
  release_audit:     'read-only',
  smoketest:         'read-only',
  debug:             'read-only',
  perf:              'read-only',
  investigate:       'read-only',
  verify_tests:      'read-only',
  // Research / planning (5): read-heavy, no code shipped
  plan:              'research',
  research:          'research',
  strategic_review:  'research',
  design_doc_update: 'research',
  memo:              'research',
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

  it('class distribution is 7 code-change + 9 read-only + 5 research = 21 total', () => {
    const counts: Record<TaskClass, number> = { 'code-change': 0, 'read-only': 0, research: 0 };
    for (const cls of Object.values(TASK_CLASS_MAP)) counts[cls]++;
    expect(counts['code-change']).toBe(7);
    expect(counts['read-only']).toBe(9);
    expect(counts['research']).toBe(5);
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

  it('handlers.memo completes with COMPLETED for a regression_false_positive dispatcher payload (no Claude call, credential-free)', async () => {
    const payload = {
      reasoning_kind: 'regression_false_positive',
      source_task_id: 'task-bake-verify',
      source_finding: {
        verdict_reason: 'CPU spike is a deployment artifact, not a regression.',
        regression_class: 'cpu_p99_spike',
      },
    };
    const task = { title: 'Bake smoke memo', description: 'Memo smoke', context: {} };

    await handlers.memo('bake-smoke-001', payload, task, '', 'owner', 'repo', null, { value: '' });

    expectCalledWith(persistAndPostStatus,
      'bake-smoke-001',
      expect.objectContaining({
        status: 'COMPLETED',
        logs: expect.stringContaining('CPU spike is a deployment artifact') as string,
      }),
    );
    // runClaudeCode must NOT be called — dispatcher path synthesizes directly
    const { runClaudeCode } = await import('../index.js');
    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
  });
});
