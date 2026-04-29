import { vi, describe, it, expect } from 'vitest';

// ── Module mocks ──────────────────────────────────────────────────────────────
// Mock ../index.js (the main agent runner) to prevent main() from executing
// and to supply the named exports that handler files import at module level.
vi.mock('../index.js', () => ({
  WORKSPACE: '/tmp/workspace',
  strVal: (v: unknown, fallback = '') => (typeof v === 'string' ? v : fallback),
  sanitize: (s: string) => s,
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
  lintFixLoop: vi.fn(),
  testFixLoop: vi.fn(),
  cloneWithRetry: vi.fn(),
  sendHeartbeatSync: vi.fn(),
}));

vi.mock('../workspacePrep.js', () => ({
  prepareWorkspaceForRole: vi.fn().mockResolvedValue(undefined),
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
import { handlers } from './index.js';

// ── Handler registry tests ────────────────────────────────────────────────────
describe('handlers registry', () => {
  const EXPECTED_TYPES = ALL_AGENT_TASK_TYPES;

  it('has exactly 18 handler entries', () => {
    expect(Object.keys(handlers)).toHaveLength(18);
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

  it('strategic_review reuses the same function reference as research', () => {
    expect(handlers.strategic_review).toBe(handlers.research);
  });

  it('all other handlers are distinct function references from each other', () => {
    // strategic_review is intentionally the same as research — skip that pair.
    const uniqueTypes = EXPECTED_TYPES.filter((t) => t !== 'strategic_review');
    const seen = new Set<unknown>();
    for (const type of uniqueTypes) {
      const fn = handlers[type];
      // Each type in this subset should have a unique function
      expect(seen.has(fn), `duplicate handler found for "${type}"`).toBe(false);
      seen.add(fn);
    }
  });
});
