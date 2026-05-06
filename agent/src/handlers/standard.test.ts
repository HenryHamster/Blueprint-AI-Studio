import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── Module mocks ──────────────────────────────────────────────────────────────
// 2026-05-04: standard.ts no longer drives lint/test/push/PR via wrapper helpers
// (lintFixLoop / testFixLoop / pushAndCreatePR are still exported from index.ts
// but unused by these handlers). The handlers now build the prompt and delegate
// to runCodeChangeTask, which runs Claude in a single session that owns its own
// ./check + ./pr + ./done + ./fail loop. The wrapper just inspects marker files
// post-Claude and posts terminal status.
//
// Tests therefore:
//   • Mock runCodeChangeTask (default no-op) and assert on its arguments
//     (especially `prompt`) for handler-level behaviour.
//   • Mock fs + index helpers for the marker-detection unit tests at the
//     bottom that exercise runCodeChangeTask itself.
vi.mock('../index.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../index.js')>();
  return {
    ...actual,
    WORKSPACE: '/tmp/workspace',
    sanitize: (s: string) => s,
    sendLog: vi.fn(),
    log: vi.fn(),
    getAgentToken: vi.fn().mockReturnValue(''),
    apiPost: vi.fn(),
    apiGet: vi.fn(),
    persistAndPostStatus: vi.fn().mockResolvedValue(undefined),
    readHandlerOutput: vi.fn(),
    validateBranchName: vi.fn((b: string, _label: string) => b),
    git: vi.fn().mockReturnValue(''),
    gitCommit: vi.fn().mockReturnValue(true),
    setupWorkspace: vi.fn().mockResolvedValue({ workBranch: 'task-abc123', shortId: 'abc123' }),
    runClaudeCode: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' }),
    captureTestBaseline: vi.fn().mockResolvedValue(null),
    sendHeartbeatSync: vi.fn(),
    getProviderRunner: vi.fn().mockReturnValue({ displayName: 'Claude Code' }),
  };
});

vi.mock('../prompt.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../prompt.js')>();
  return {
    ...actual,
    getTaskTypeHints: vi.fn().mockReturnValue({}),
    readClaudeMd: vi.fn().mockReturnValue(''),
    buildContextPrefix: vi.fn().mockReturnValue(''),
    buildFileTree: vi.fn().mockReturnValue(''),
    discoverRelatedTests: vi.fn().mockReturnValue(''),
    configureAgentPermissions: vi.fn().mockReturnValue([]),
    writePlaybookHelper: vi.fn(),
    writeMessagesHelper: vi.fn(),
    writeCodeChangeHelpers: vi.fn(),
    formatPlaybook: vi.fn().mockReturnValue(''),
    buildRetryContext: vi.fn().mockReturnValue(''),
    buildAnalysisPrompt: vi.fn().mockReturnValue('analysis-prompt'),
    parseAnalysisPlanFiles: vi.fn().mockReturnValue([]),
    parseAnalysisPlanTestFiles: vi.fn().mockReturnValue([]),
    parseAnalysisPlanVerifyFiles: vi.fn().mockReturnValue([]),
    extractModuleInterfaces: vi.fn().mockReturnValue(''),
    discoverAffectedConsumers: vi.fn().mockReturnValue(''),
  };
});

// Mock the code-change module so handler tests can assert on what was passed
// to runCodeChangeTask without exercising the marker-detection branches. The
// marker tests at the bottom unmock + re-import the real implementation.
vi.mock('./code-change.js', () => ({
  runCodeChangeTask: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('../build-config.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../build-config.js')>();
  return { ...actual };
});

// fs is consumed by both `import fs from 'fs'` (default import) and
// `import * as fs from 'fs'` (namespace import) across the codebase under
// test. Mock both shapes so vitest doesn't reject either access pattern.
vi.mock('fs', () => {
  const stub = {
    readFileSync: vi.fn().mockReturnValue(''),
    writeFileSync: vi.fn(),
    existsSync: vi.fn().mockReturnValue(false),
    appendFileSync: vi.fn(),
    mkdirSync: vi.fn(),
    readdirSync: vi.fn().mockReturnValue([]),
    unlinkSync: vi.fn(),
  };
  return { ...stub, default: stub };
});

vi.mock('child_process', () => ({
  execFileSync: vi.fn().mockReturnValue(Buffer.from('')),
  execFile: vi.fn(),
  execSync: vi.fn().mockReturnValue(''),
  spawn: vi.fn().mockReturnValue({
    stderr: { on: vi.fn() },
    stdout: { on: vi.fn() },
    on: vi.fn(),
    kill: vi.fn(),
  }),
}));

vi.mock('ws', () => ({
  default: class MockWebSocket {
    static OPEN = 1;
    readyState = 0;
    on = vi.fn();
    send = vi.fn();
    close = vi.fn();
  },
}));

// ── Imports under test ────────────────────────────────────────────────────────
import { handleCreate, handleRefactor, handleDocs, handleQA, handleDeps, extractSelfReviewJson, needsMigrationGeneration, buildSmallScopeCallerNote, runFreshReviewerPass } from './standard.js';
import { setupWorkspace, captureTestBaseline, runClaudeCode } from '../index.js';
import { extractModuleInterfaces, discoverAffectedConsumers, parseAnalysisPlanFiles, parseAnalysisPlanTestFiles, parseAnalysisPlanVerifyFiles, buildAnalysisPrompt } from '../prompt.js';
import { runCodeChangeTask } from './code-change.js';

// ── Shared test helpers ───────────────────────────────────────────────────────
const TASK_ID = 'task-123';
const GH_TOKEN = 'gh-token';
const OWNER = 'owner';
const REPO = 'repo';
const WS = null;
const STATUS_REF = () => ({ value: '' });

/** Returns the prompt passed to the latest runCodeChangeTask invocation. */
function capturedPrompt(): string {
  const calls = vi.mocked(runCodeChangeTask).mock.calls;
  if (calls.length === 0) throw new Error('runCodeChangeTask was not invoked');
  return calls[calls.length - 1][0].prompt;
}

/** Returns the full opts object passed to the latest runCodeChangeTask invocation. */
function capturedOpts(): Parameters<typeof runCodeChangeTask>[0] {
  const calls = vi.mocked(runCodeChangeTask).mock.calls;
  if (calls.length === 0) throw new Error('runCodeChangeTask was not invoked');
  return calls[calls.length - 1][0];
}

// ── Overlap warning text generation ──────────────────────────────────────────
describe('overlap warning text generation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('generates no warning when file_overlap_warnings is absent', async () => {
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).not.toContain('WARNING:');
  });

  it('generates no warning when file_overlap_warnings is an empty array', async () => {
    const task = {
      title: 'Task',
      description: '',
      context: { file_overlap_warnings: [] },
      project_context: {},
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).not.toContain('WARNING:');
  });

  it('generates a warning containing shared files and task name for a single overlap', async () => {
    const task = {
      title: 'Task',
      description: '',
      context: {
        file_overlap_warnings: [
          { shared_files: ['src/foo.ts', 'src/bar.ts'], with_task: 'task-parallel' },
        ],
      },
      project_context: {},
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).toContain('WARNING:');
    expect(prompt).toContain('src/foo.ts, src/bar.ts');
    expect(prompt).toContain('task-parallel');
  });

  it('generates separate warnings for each entry when multiple overlaps exist', async () => {
    const task = {
      title: 'Task',
      description: '',
      context: {
        file_overlap_warnings: [
          { shared_files: ['src/a.ts'], with_task: 'task-alpha' },
          { shared_files: ['src/b.ts'], with_task: 'task-beta' },
        ],
      },
      project_context: {},
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).toContain('task-alpha');
    expect(prompt).toContain('task-beta');
  });

  it('applies the same overlap warning logic in handleRefactor', async () => {
    const task = {
      title: 'Refactor task',
      description: '',
      context: {
        file_overlap_warnings: [
          { shared_files: ['src/utils.ts'], with_task: 'task-concurrent' },
        ],
      },
    };
    await handleRefactor(TASK_ID, { prompt: 'Refactor it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).toContain('WARNING:');
    expect(prompt).toContain('src/utils.ts');
    expect(prompt).toContain('task-concurrent');
  });
});

// ── Sibling task note generation ──────────────────────────────────────────────
describe('sibling task note generation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('omits sibling note when sibling_tasks array is empty', async () => {
    const task = {
      title: 'Task',
      description: '',
      context: {},
      project_context: { sibling_tasks: [] },
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).not.toContain('avoid making changes that overlap');
  });

  it('appends sibling note when sibling_tasks is non-empty', async () => {
    const task = {
      title: 'Task',
      description: '',
      context: {},
      project_context: { sibling_tasks: [{ title: 'Parallel task' }] },
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).toContain('avoid making changes that overlap');
  });

  it('omits sibling note when project_context is not present on task', async () => {
    const task = { title: 'Task', description: '' };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).not.toContain('avoid making changes that overlap');
  });
});

// ── Handler configuration: branchPrefix and runCodeChangeTask wiring ─────────
describe('handleCreate configuration', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('uses branchPrefix "task"', async () => {
    const task = { title: 'Task', description: '' };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(setupWorkspace)).toHaveBeenCalledWith(
      OWNER, REPO, GH_TOKEN, 'main', 'task', TASK_ID, WS, expect.any(Object), undefined,
    );
  });

  it('throws when no prompt is found in payload or task', async () => {
    const task = { title: '', description: '' };
    await expect(
      handleCreate(TASK_ID, {}, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF()),
    ).rejects.toThrow('No prompt found');
  });

  it('passes the resolved workBranch and baseBranch through to runCodeChangeTask', async () => {
    const task = { title: 'Task', description: '' };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const opts = capturedOpts();
    expect(opts.taskId).toBe(TASK_ID);
    expect(opts.workBranch).toBe('task-abc123');
    expect(opts.baseBranch).toBe('main');
    expect(opts.owner).toBe(OWNER);
    expect(opts.repo).toBe(REPO);
    expect(opts.testsOnMainBranch).toBe(true);
  });

  it('passes a custom base_branch through to runCodeChangeTask and disables tests-on-main', async () => {
    const task = { title: 'Task', description: '' };
    await handleCreate(TASK_ID, { prompt: 'Do something', base_branch: 'feature/parent' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const opts = capturedOpts();
    expect(opts.baseBranch).toBe('feature/parent');
    expect(opts.testsOnMainBranch).toBe(false);
  });
});

describe('handleRefactor configuration', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('uses branchPrefix "refactor"', async () => {
    const task = { title: 'Refactor task', description: '' };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(setupWorkspace)).toHaveBeenCalledWith(
      OWNER, REPO, GH_TOKEN, 'main', 'refactor', TASK_ID, WS, expect.any(Object), undefined,
    );
  });

  it('includes REFACTOR TASK strict constraints in prompt', async () => {
    const task = { title: 'Refactor task', description: '' };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).toContain('REFACTOR TASK — STRICT CONSTRAINTS');
  });
});

// ── Module interface extraction (scope-conditional) ───────────────────────────
describe('module interface extraction', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(extractModuleInterfaces).mockReturnValue('## Module Interfaces\nfoo\n\n');
  });

  it('calls extractModuleInterfaces for handleCreate with scope=medium', async () => {
    const task = { title: 'Task', description: '', context: { scope: 'medium' } };
    await handleCreate(TASK_ID, { prompt: 'Do it', related_files: ['src/foo.ts'] }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(extractModuleInterfaces)).toHaveBeenCalledWith(WS, ['src/foo.ts']);
    expect(capturedPrompt()).toContain('## Module Interfaces');
  });

  it('calls extractModuleInterfaces for handleCreate with scope=large', async () => {
    const task = { title: 'Task', description: '', context: { scope: 'large' } };
    await handleCreate(TASK_ID, { prompt: 'Do it', related_files: ['src/bar.ts'] }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(extractModuleInterfaces)).toHaveBeenCalledWith(WS, ['src/bar.ts']);
  });

  it('does not call extractModuleInterfaces for handleCreate with scope=small', async () => {
    const task = { title: 'Task', description: '', context: { scope: 'small' } };
    await handleCreate(TASK_ID, { prompt: 'Do it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(extractModuleInterfaces)).not.toHaveBeenCalled();
  });

  it('does not call extractModuleInterfaces for handleCreate with no scope', async () => {
    const task = { title: 'Task', description: '' };
    await handleCreate(TASK_ID, { prompt: 'Do it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(extractModuleInterfaces)).not.toHaveBeenCalled();
  });

  it('calls extractModuleInterfaces for handleRefactor with scope=medium', async () => {
    const task = { title: 'Refactor', description: '', context: { scope: 'medium' } };
    await handleRefactor(TASK_ID, { prompt: 'Improve it', related_files: ['src/utils.ts'] }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(extractModuleInterfaces)).toHaveBeenCalledWith(WS, ['src/utils.ts']);
    expect(capturedPrompt()).toContain('## Module Interfaces');
  });

  it('does not call extractModuleInterfaces for handleRefactor with scope=small', async () => {
    const task = { title: 'Refactor', description: '', context: { scope: 'small' } };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(extractModuleInterfaces)).not.toHaveBeenCalled();
  });
});

// ── Affected consumer discovery (scope-conditional) ───────────────────────────
describe('affected consumer discovery', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(discoverAffectedConsumers).mockReturnValue('## Affected Consumers\nbar\n\n');
  });

  it('calls discoverAffectedConsumers for handleCreate with scope=medium', async () => {
    const task = { title: 'Task', description: '', context: { scope: 'medium' } };
    await handleCreate(TASK_ID, { prompt: 'Do it', related_files: ['src/foo.ts'] }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(discoverAffectedConsumers)).toHaveBeenCalledWith(WS, ['src/foo.ts']);
    expect(capturedPrompt()).toContain('## Affected Consumers');
  });

  it('calls discoverAffectedConsumers for handleCreate with scope=large', async () => {
    const task = { title: 'Task', description: '', context: { scope: 'large' } };
    await handleCreate(TASK_ID, { prompt: 'Do it', related_files: ['src/bar.ts'] }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(discoverAffectedConsumers)).toHaveBeenCalledWith(WS, ['src/bar.ts']);
  });

  it('does not call discoverAffectedConsumers for handleCreate with scope=small', async () => {
    const task = { title: 'Task', description: '', context: { scope: 'small' } };
    await handleCreate(TASK_ID, { prompt: 'Do it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(discoverAffectedConsumers)).not.toHaveBeenCalled();
  });

  it('does not call discoverAffectedConsumers for handleCreate with no scope', async () => {
    const task = { title: 'Task', description: '' };
    await handleCreate(TASK_ID, { prompt: 'Do it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(discoverAffectedConsumers)).not.toHaveBeenCalled();
  });

  it('calls discoverAffectedConsumers for handleRefactor with scope=medium', async () => {
    const task = { title: 'Refactor', description: '', context: { scope: 'medium' } };
    await handleRefactor(TASK_ID, { prompt: 'Improve it', related_files: ['src/utils.ts'] }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(discoverAffectedConsumers)).toHaveBeenCalledWith(WS, ['src/utils.ts']);
    expect(capturedPrompt()).toContain('## Affected Consumers');
  });

  it('does not call discoverAffectedConsumers for handleRefactor with scope=small', async () => {
    const task = { title: 'Refactor', description: '', context: { scope: 'small' } };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(discoverAffectedConsumers)).not.toHaveBeenCalled();
  });
});

// ── Pre-implementation analysis note (small + medium scope) ───────────────────
describe('pre-implementation analysis note', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('injects small-scope Pre-Implementation Analysis prefix for handleCreate with scope=small', async () => {
    const task = { title: 'Task', description: '', context: { scope: 'small' } };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).toContain('## Pre-Implementation Analysis');
    expect(prompt).toContain('under ~50 lines across 1–3 files');
    // Small-scope adds only the prefix — no separate Claude Code analysis run.
    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
  });

  it('does not inject Pre-Implementation Analysis for handleCreate with scope=medium without analysis result', async () => {
    // analysis call returns empty string → no Pre-Implementation Analysis
    // section is prepended.
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' });
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).not.toContain('## Pre-Implementation Analysis');
  });

  it('does not inject Pre-Implementation Analysis for handleCreate with no scope', async () => {
    const task = { title: 'Task', description: '' };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).not.toContain('## Pre-Implementation Analysis');
  });

  it('does not inject Pre-Implementation Analysis for handleDocs', async () => {
    const task = { title: 'Docs task', description: '', context: { scope: 'small' } };
    await handleDocs(TASK_ID, {}, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).not.toContain('## Pre-Implementation Analysis');
  });

  it('does not inject Pre-Implementation Analysis for handleQA', async () => {
    const task = { title: 'QA task', description: '', context: { scope: 'small' } };
    await handleQA(TASK_ID, {}, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).not.toContain('## Pre-Implementation Analysis');
  });

  it('reuses _previous_analysis_plan on medium-scope retry and skips the analysis Claude Code call', async () => {
    const priorPlan = 'Module X imports Y. Plan: change files A, B.';
    const task = {
      title: 'Task',
      description: '',
      context: { scope: 'medium', _previous_analysis_plan: priorPlan },
      project_context: {},
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // No analysis Claude Code call should fire — plan is reused verbatim.
    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    const prompt = capturedPrompt();
    expect(prompt).toContain('## Pre-Implementation Analysis');
    expect(prompt).toContain(priorPlan);
  });

  it('preserves existing Pre-Implementation Analysis header when reusing a previous plan', async () => {
    const priorPlan = '## Pre-Implementation Analysis\n\nModule X imports Y.';
    const task = {
      title: 'Task',
      description: '',
      context: { scope: 'medium', _previous_analysis_plan: priorPlan },
      project_context: {},
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    // Header appears exactly once (no duplicate prefix when the stored plan already includes it).
    expect(prompt.match(/## Pre-Implementation Analysis/g) ?? []).toHaveLength(1);
    expect(prompt).toContain('Module X imports Y.');
  });
});

describe('handleDocs configuration', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('uses branchPrefix "docs"', async () => {
    const task = { title: 'Docs task', description: '' };
    await handleDocs(TASK_ID, {}, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(setupWorkspace)).toHaveBeenCalledWith(
      OWNER, REPO, GH_TOKEN, 'main', 'docs', TASK_ID, WS, expect.any(Object), undefined,
    );
  });

  it('includes documentation agent instructions in prompt', async () => {
    const task = { title: 'Docs task', description: '' };
    await handleDocs(TASK_ID, {}, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).toContain('documentation agent');
  });
});

describe('handleQA configuration', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('uses branchPrefix "task"', async () => {
    const task = { title: 'QA task', description: '' };
    await handleQA(TASK_ID, {}, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(setupWorkspace)).toHaveBeenCalledWith(
      OWNER, REPO, GH_TOKEN, 'main', 'task', TASK_ID, WS, expect.any(Object), undefined,
    );
  });

  it('includes QA agent instructions in prompt', async () => {
    const task = { title: 'QA task', description: '' };
    await handleQA(TASK_ID, {}, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).toContain('QA agent');
  });
});

describe('handleDeps configuration', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('uses branchPrefix "deps"', async () => {
    const task = { title: 'Deps task', description: '' };
    await handleDeps(TASK_ID, {}, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(setupWorkspace)).toHaveBeenCalledWith(
      OWNER, REPO, GH_TOKEN, 'main', 'deps', TASK_ID, WS, expect.any(Object), undefined,
    );
  });

  it('includes dependency update agent instructions in prompt', async () => {
    const task = { title: 'Deps task', description: '' };
    await handleDeps(TASK_ID, {}, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).toContain('dependency update agent');
  });
});

// ── Code-change helper guidance ───────────────────────────────────────────────
// The prompt now MUST tell Claude about ./check + ./pr + ./done + ./fail. If
// CODE_CHANGE_HELPER_GUIDANCE drops out of runStandardHandler, Claude has no
// way to ship its work — silent regression that would only show up in
// production. Lock it in.
describe('code-change helper guidance is in prompt', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('includes ./check, ./pr, ./done, ./fail guidance for handleCreate', async () => {
    const task = { title: 'Task', description: '' };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).toContain('./check');
    expect(prompt).toContain('./pr');
    expect(prompt).toContain('./done');
    expect(prompt).toContain('./fail');
  });

  it('includes ./check, ./pr, ./done, ./fail guidance for handleRefactor', async () => {
    const task = { title: 'Refactor task', description: '' };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).toContain('./check');
    expect(prompt).toContain('./pr');
    expect(prompt).toContain('./done');
    expect(prompt).toContain('./fail');
  });

  it('includes ./check, ./pr, ./done, ./fail guidance for handleDocs', async () => {
    const task = { title: 'Docs task', description: '' };
    await handleDocs(TASK_ID, {}, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).toContain('./check');
    expect(prompt).toContain('./pr');
    expect(prompt).toContain('./done');
    expect(prompt).toContain('./fail');
  });

  it('includes ./check, ./pr, ./done, ./fail guidance for handleQA', async () => {
    const task = { title: 'QA task', description: '' };
    await handleQA(TASK_ID, {}, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).toContain('./check');
    expect(prompt).toContain('./pr');
    expect(prompt).toContain('./done');
    expect(prompt).toContain('./fail');
  });

  it('includes ./check, ./pr, ./done, ./fail guidance for handleDeps', async () => {
    const task = { title: 'Deps task', description: '' };
    await handleDeps(TASK_ID, {}, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).toContain('./check');
    expect(prompt).toContain('./pr');
    expect(prompt).toContain('./done');
    expect(prompt).toContain('./fail');
  });
});

// ── Test baseline capture ─────────────────────────────────────────────────────
// Baseline still gates on baseBranch === 'main' && captureBaseline && runTestFix
// && !auto_remediation. handleCreate sets captureBaseline + runTestFix true;
// handleRefactor sets captureBaseline true. handleDocs/QA/Deps don't.
describe('test baseline capture', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('calls captureTestBaseline for medium scope create tasks on main', async () => {
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).toHaveBeenCalled();
  });

  it('calls captureTestBaseline for large scope create tasks on main', async () => {
    const task = { title: 'Task', description: '', context: { scope: 'large' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).toHaveBeenCalled();
  });

  it('calls captureTestBaseline for small scope create tasks on main', async () => {
    const task = { title: 'Task', description: '', context: { scope: 'small' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).toHaveBeenCalled();
  });

  it('calls captureTestBaseline when scope is not set', async () => {
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).toHaveBeenCalled();
  });

  it('passes related_files from payload to captureTestBaseline', async () => {
    const relatedFiles = ['agent/src/index.ts', 'agent/src/handlers/standard.ts'];
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something', related_files: relatedFiles }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).toHaveBeenCalledWith(
      WS, expect.any(Object), undefined, relatedFiles,
    );
  });

  it('skips captureTestBaseline for auto-remediation tasks', async () => {
    const task = { title: 'Task', description: '', context: { auto_remediation: true }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).not.toHaveBeenCalled();
  });

  it('skips captureTestBaseline when base_branch is not main', async () => {
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something', base_branch: 'feature/x' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).not.toHaveBeenCalled();
  });

  it('does not call captureTestBaseline for handleDocs (runTestFix=false)', async () => {
    const task = { title: 'Docs task', description: '' };
    await handleDocs(TASK_ID, {}, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).not.toHaveBeenCalled();
  });

  it('does not call captureTestBaseline for handleQA (runTestFix=false)', async () => {
    const task = { title: 'QA task', description: '' };
    await handleQA(TASK_ID, {}, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).not.toHaveBeenCalled();
  });

  it('does not call captureTestBaseline for handleDeps (runTestFix=false)', async () => {
    const task = { title: 'Deps task', description: '' };
    await handleDeps(TASK_ID, {}, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).not.toHaveBeenCalled();
  });

  it('writes test_baseline file when captureTestBaseline returns a non-null set', async () => {
    const { writeFileSync } = await import('fs');
    vi.mocked(captureTestBaseline).mockResolvedValue(new Set(['test A', 'test B']));
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const baselineCalls = vi.mocked(writeFileSync).mock.calls.filter(c => String(c[0]).includes('test_baseline_'));
    expect(baselineCalls).toHaveLength(1);
    expect(baselineCalls[0][0]).toBe(`/tmp/test_baseline_${TASK_ID}.json`);
    expect(baselineCalls[0][1]).toContain('test A');
  });

  it('does not write test_baseline file when captureTestBaseline returns null', async () => {
    const { writeFileSync } = await import('fs');
    vi.mocked(writeFileSync).mockClear();
    vi.mocked(captureTestBaseline).mockResolvedValue(null);
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const baselineCalls = vi.mocked(writeFileSync).mock.calls.filter(c => String(c[0]).includes('test_baseline_'));
    expect(baselineCalls).toHaveLength(0);
  });
});

// ── Two-phase analysis for medium-scope tasks ────────────────────────────────
describe('two-phase analysis for medium-scope tasks', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('runs analysis phase before main invocation for medium-scope create tasks', async () => {
    const task = {
      title: 'Task',
      description: '',
      context: { scope: 'medium' },
      project_context: {},
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // Exactly one analysis Claude Code invocation; the implementation pass is
    // delegated to runCodeChangeTask.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
    const analysisCallArgs = vi.mocked(runClaudeCode).mock.calls[0];
    // Analysis call passes disallowedTools including Write, Edit, NotebookEdit
    expect(analysisCallArgs[5]).toEqual(expect.arrayContaining(['Write', 'Edit', 'NotebookEdit']));
    // And runCodeChangeTask was invoked exactly once (the "main" pass).
    expect(vi.mocked(runCodeChangeTask)).toHaveBeenCalledTimes(1);
  });

  it('skips analysis phase when scope is "small"', async () => {
    const task = {
      title: 'Task',
      description: '',
      context: { scope: 'small' },
      project_context: {},
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    expect(vi.mocked(runCodeChangeTask)).toHaveBeenCalledTimes(1);
  });

  it('skips analysis phase when scope is not set', async () => {
    const task = {
      title: 'Task',
      description: '',
      context: {},
      project_context: {},
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    expect(vi.mocked(runCodeChangeTask)).toHaveBeenCalledTimes(1);
  });

  it('prepends analysis result to main prompt when analysis succeeds', async () => {
    const analysisOutput = 'Files to change: foo.ts\nInterfaces to preserve: Foo interface';
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: analysisOutput });
    const task = {
      title: 'Task',
      description: '',
      context: { scope: 'medium' },
      project_context: {},
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const mainPrompt = capturedPrompt();
    expect(mainPrompt).toContain('Pre-Implementation Analysis');
    expect(mainPrompt).toContain(analysisOutput);
  });

  it('proceeds without analysis context when analysis exits non-zero', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 1, allOutput: 'error', lastResultText: '' });
    const task = {
      title: 'Task',
      description: '',
      context: { scope: 'medium' },
      project_context: {},
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).not.toContain('Pre-Implementation Analysis');
  });

  it('proceeds without analysis context when analysis returns empty output', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '   ' });
    const task = {
      title: 'Task',
      description: '',
      context: { scope: 'medium' },
      project_context: {},
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).not.toContain('Pre-Implementation Analysis');
  });

  it('runs analysis phase for medium-scope refactor tasks', async () => {
    const task = {
      title: 'Refactor task',
      description: '',
      context: { scope: 'medium' },
    };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
    const analysisCallArgs = vi.mocked(runClaudeCode).mock.calls[0];
    expect(analysisCallArgs[5]).toEqual(expect.arrayContaining(['Write', 'Edit', 'NotebookEdit']));
  });

  it('passes analysisContext through to runCodeChangeTask when medium-scope analysis succeeds', async () => {
    const analysisOutput = '**Key dependencies**\nfoo.ts depends on bar.ts';
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: analysisOutput });
    const task = {
      title: 'Task',
      description: '',
      context: { scope: 'medium' },
      project_context: {},
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const opts = capturedOpts();
    expect(opts.analysisContext).toBeDefined();
    expect(opts.analysisContext).toContain('Pre-Implementation Analysis');
  });

  it('passes empty analysisContext through to runCodeChangeTask when analysis fails', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 1, allOutput: 'error', lastResultText: '' });
    const task = {
      title: 'Task',
      description: '',
      context: { scope: 'medium' },
      project_context: {},
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const opts = capturedOpts();
    expect(opts.analysisContext).toBe('');
  });
});

// ── Analysis-phase scope feasibility gate ────────────────────────────────────
describe('analysis-phase scope feasibility gate', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(parseAnalysisPlanFiles).mockReset();
    vi.mocked(parseAnalysisPlanTestFiles).mockReset();
    vi.mocked(parseAnalysisPlanTestFiles).mockReturnValue([]);
    vi.mocked(parseAnalysisPlanVerifyFiles).mockReset();
    vi.mocked(parseAnalysisPlanVerifyFiles).mockReturnValue([]);
  });

  it('proceeds to implementation when planned files are within the limit', async () => {
    const analysisOutput = '**Files to change**\n- a.ts\n- b.ts';
    vi.mocked(parseAnalysisPlanFiles).mockReturnValue(['a.ts', 'b.ts']);
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: analysisOutput });
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // Exactly one analysis Claude Code run; no narrowing retry. Then
    // runCodeChangeTask runs the implementation.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(runCodeChangeTask)).toHaveBeenCalledTimes(1);
  });

  it('passes cochange_files from project_context through to buildAnalysisPrompt', async () => {
    vi.mocked(parseAnalysisPlanFiles).mockReturnValue(['a.ts']);
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '**Files to change**\n- a.ts' });
    const task = {
      title: 'Task',
      description: '',
      context: { scope: 'medium' },
      project_context: { cochange_files: ['x/y.ts', 'p/q.ts'] },
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(buildAnalysisPrompt)).toHaveBeenCalledWith(
      expect.objectContaining({ cochangeFiles: ['x/y.ts', 'p/q.ts'] }),
    );
  });

  it('retries analysis with narrowing guidance when plan exceeds the limit', async () => {
    const overflowFiles = ['a.ts', 'b.ts', 'c.ts', 'd.ts', 'e.ts', 'f.ts', 'g.ts', 'h.ts', 'i.ts', 'j.ts', 'k.ts'];
    const narrowedFiles = ['a.ts', 'b.ts'];
    vi.mocked(parseAnalysisPlanFiles)
      .mockReturnValueOnce(overflowFiles)
      .mockReturnValueOnce(narrowedFiles);
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '**Files to change**\n- a.ts' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '**Files to change**\n- a.ts\n- b.ts' });
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // analysis + narrowing retry = 2 runClaudeCode calls; implementation runs via runCodeChangeTask
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(runCodeChangeTask)).toHaveBeenCalledTimes(1);
    const { persistAndPostStatus } = await import('../index.js');
    expect(vi.mocked(persistAndPostStatus)).not.toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({ failure_category: 'scope_overflow' }),
    );
  });

  it('fails fast with scope_overflow when narrowing retry still exceeds the limit', async () => {
    const overflow = ['a.ts', 'b.ts', 'c.ts', 'd.ts', 'e.ts', 'f.ts', 'g.ts', 'h.ts', 'i.ts', 'j.ts', 'k.ts'];
    vi.mocked(parseAnalysisPlanFiles).mockReturnValue(overflow);
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '**Files to change**\n- a.ts' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '**Files to change**\n- a.ts' });
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const { persistAndPostStatus } = await import('../index.js');
    expect(vi.mocked(persistAndPostStatus)).toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({
        status: 'FAILED',
        failure_category: 'scope_overflow',
      }),
    );
    const failureCall = vi.mocked(persistAndPostStatus).mock.calls.find(
      ([, body]) => body.failure_category === 'scope_overflow',
    );
    expect(failureCall).toBeDefined();
    if (failureCall) {
      const body = failureCall[1];
      expect(typeof body.analysis_plan).toBe('string');
      expect(body.scope_gate_triggered).toBe('analysis_overflow');
    }
    // implementation must NOT have happened
    expect(vi.mocked(runCodeChangeTask)).not.toHaveBeenCalled();
  });

  it('does not count test files toward the production-file budget', async () => {
    // 6 production files (≤ production limit 10) + many test files: should pass without narrowing.
    const production = ['a.ts', 'b.ts', 'c.ts', 'd.ts', 'e.ts', 'f.ts'];
    const tests = ['a.test.ts', 'b.test.ts', 'c.test.ts', 'd.test.ts', 'e.test.ts', 'f.test.ts'];
    vi.mocked(parseAnalysisPlanFiles).mockReturnValue(production);
    vi.mocked(parseAnalysisPlanTestFiles).mockReturnValue(tests);
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '**Files to change**\n- a.ts' });
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // Exactly one analysis run; no narrowing retry triggered by tests.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(runCodeChangeTask)).toHaveBeenCalledTimes(1);
    const { persistAndPostStatus } = await import('../index.js');
    expect(vi.mocked(persistAndPostStatus)).not.toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({ failure_category: 'scope_overflow' }),
    );
  });

  it('does not count verify-only files toward the production-file budget', async () => {
    const production = ['a.ts', 'b.ts', 'c.ts'];
    const verify = ['v1.ts', 'v2.ts', 'v3.ts', 'v4.ts', 'v5.ts', 'v6.ts', 'v7.ts', 'v8.ts'];
    vi.mocked(parseAnalysisPlanFiles).mockReturnValue(production);
    vi.mocked(parseAnalysisPlanTestFiles).mockReturnValue([]);
    vi.mocked(parseAnalysisPlanVerifyFiles).mockReturnValue(verify);
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '**Files to change**\n- a.ts' });
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(runCodeChangeTask)).toHaveBeenCalledTimes(1);
    const { persistAndPostStatus } = await import('../index.js');
    expect(vi.mocked(persistAndPostStatus)).not.toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({ failure_category: 'scope_overflow' }),
    );
  });
});

// ── Issue context flows into the prompt ───────────────────────────────────────
// PR body construction (with `Closes #N` etc.) now lives inside Claude's ./pr
// invocation — we can no longer assert on the rendered PR body. What we can
// (and should) assert is that the issue context is part of the prompt the
// agent sees, so the agent has the information needed to construct the body.
describe('issue context propagation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('includes the user prompt in the runCodeChangeTask prompt', async () => {
    const task = { title: 'Task A', description: '', context: { issue_number: 42 } };
    await handleCreate(TASK_ID, { prompt: 'Wire up the new endpoint' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).toContain('Wire up the new endpoint');
  });

  it('includes a task title prefix when one is set', async () => {
    const task = { title: 'My Task', description: '' };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).toContain('Task: My Task');
  });
});

// ── buildSmallScopeCallerNote (unchanged unit tests) ──────────────────────────
describe('buildSmallScopeCallerNote', () => {
  it('returns empty string when no related files are provided', () => {
    expect(buildSmallScopeCallerNote([])).toBe('');
  });

  it('returns empty string when target path cannot be read', () => {
    expect(buildSmallScopeCallerNote(['does/not/exist-xyz-99.ts'])).toBe('');
  });

  it('rejects suspicious paths containing ..', () => {
    expect(buildSmallScopeCallerNote(['../etc/passwd'])).toBe('');
  });

  it('iterates over multiple related files and labels primary vs related', async () => {
    const { execFileSync } = await import('child_process');
    const mocked = vi.mocked(execFileSync);
    mocked.mockImplementation(((cmd: string, args?: readonly string[]) => {
      if (cmd === 'cat') {
        const path = args?.[0] || '';
        if (path.endsWith('/a.ts')) return 'export function alpha() {}\n' as unknown as Buffer;
        if (path.endsWith('/b.ts')) return 'export class Beta {}\n' as unknown as Buffer;
        throw new Error('not found');
      }
      if (cmd === 'git') {
        const pattern = (args || []).find(a => typeof a === 'string' && a.includes('import'));
        if (pattern?.includes('\\ba\\b')) return 'some/caller-a.ts\n' as unknown as Buffer;
        if (pattern?.includes('\\bb\\b')) return 'some/caller-b.ts\n' as unknown as Buffer;
        return '' as unknown as Buffer;
      }
      return '' as unknown as Buffer;
    }) as typeof execFileSync);

    const note = buildSmallScopeCallerNote(['pkg/a.ts', 'pkg/b.ts']);
    expect(note).toContain('Primary target `pkg/a.ts` exposes: alpha.');
    expect(note).toContain('Related file `pkg/b.ts` exposes: Beta.');
    expect(note).toContain('Files that reference `pkg/a.ts`: `some/caller-a.ts`.');
    expect(note).toContain('Files that reference `pkg/b.ts`: `some/caller-b.ts`.');
    expect(note).toContain('Preserve these exports and update any callers you change.');
    mocked.mockReturnValue(Buffer.from(''));
  });

  it('caps iteration at 3 related files', async () => {
    const { execFileSync } = await import('child_process');
    const mocked = vi.mocked(execFileSync);
    const seen: string[] = [];
    mocked.mockImplementation(((cmd: string, args?: readonly string[]) => {
      if (cmd === 'cat' && args?.[0]) seen.push(args[0]);
      return '' as unknown as Buffer;
    }) as typeof execFileSync);

    buildSmallScopeCallerNote(['p/a.ts', 'p/b.ts', 'p/c.ts', 'p/d.ts']);
    expect(seen.some(p => p.endsWith('p/d.ts'))).toBe(false);
    expect(seen.filter(p => p.startsWith('/tmp/workspace/')).length).toBeLessThanOrEqual(3);
    mocked.mockReturnValue(Buffer.from(''));
  });

  it('skips unreadable files but still emits note for readable ones', async () => {
    const { execFileSync } = await import('child_process');
    const mocked = vi.mocked(execFileSync);
    mocked.mockImplementation(((cmd: string, args?: readonly string[]) => {
      if (cmd === 'cat') {
        const path = args?.[0] || '';
        if (path.endsWith('/missing.ts')) throw new Error('ENOENT');
        if (path.endsWith('/real.ts')) return 'export const readable = 1;\n' as unknown as Buffer;
      }
      return '' as unknown as Buffer;
    }) as typeof execFileSync);

    const note = buildSmallScopeCallerNote(['p/missing.ts', 'p/real.ts']);
    expect(note).toContain('`p/real.ts`');
    expect(note).not.toContain('`p/missing.ts`');
    expect(note).toContain('Preserve these exports');
    mocked.mockReturnValue(Buffer.from(''));
  });
});

// ── extractSelfReviewJson parsing (unchanged unit tests) ──────────────────────
describe('extractSelfReviewJson', () => {
  it('parses a valid ```json fenced block', () => {
    const output = '```json\n{"verdict":"LGTM","confidence":0.9}\n```';
    expect(extractSelfReviewJson(output)).toEqual({ verdict: 'LGTM', confidence: 0.9 });
  });

  it('parses fenced JSON even when surrounded by prose', () => {
    const output = 'Here is my review:\n```json\n{"ok":true}\n```\nThanks!';
    expect(extractSelfReviewJson(output)).toEqual({ ok: true });
  });

  it('prefers fenced JSON over a later unfenced object', () => {
    const output = '```json\n{"first":true}\n```\nlater {"second":true}';
    expect(extractSelfReviewJson(output)).toEqual({ first: true });
  });

  it('falls back to brace scanning when the closing fence is missing', () => {
    // No closing ``` — regex can't match, but the braces still form valid JSON.
    const output = '```json\n{"a":1}';
    expect(extractSelfReviewJson(output)).toEqual({ a: 1 });
  });

  it('recovers via brace fallback when fenced content has no parseable object', () => {
    // Fence captures non-JSON text (no braces), so fallback can safely slice
    // the outer {...} without being polluted by the broken fenced block.
    const output = '```json\nnothing here\n```\n{"valid":true}';
    expect(extractSelfReviewJson(output)).toEqual({ valid: true });
  });

  it('handles nested fence markers inside a JSON string via the brace fallback', () => {
    // The non-greedy regex stops at the first inner ``` so the fenced parse
    // fails; the fallback then slices the outermost { ... } which is valid.
    const output = '```json\n{"code":"```foo```"}\n```';
    expect(extractSelfReviewJson(output)).toEqual({ code: '```foo```' });
  });

  it('returns null when multiple JSON objects make the fallback slice unparseable', () => {
    // start = first `{`, end = last `}` — the combined slice spans prose between
    // the two objects, so JSON.parse fails and no object is returned.
    const output = '{"a":1} and {"b":2}';
    expect(extractSelfReviewJson(output)).toBeNull();
  });

  it('returns null for an empty string', () => {
    expect(extractSelfReviewJson('')).toBeNull();
  });

  it('returns null for a string containing only `{`', () => {
    expect(extractSelfReviewJson('{')).toBeNull();
  });

  it('returns null for a string containing only `}`', () => {
    expect(extractSelfReviewJson('}')).toBeNull();
  });

  it('parses JSON embedded in surrounding prose via the brace fallback', () => {
    const output = 'Review result: {"verdict":"LGTM","notes":"looks good"} — done.';
    expect(extractSelfReviewJson(output)).toEqual({ verdict: 'LGTM', notes: 'looks good' });
  });

  it('returns null when no JSON-like structure is present', () => {
    expect(extractSelfReviewJson('no json here at all')).toBeNull();
  });
});

describe('needsMigrationGeneration', () => {
  it('returns false when models.py is not in changedPaths', () => {
    expect(needsMigrationGeneration(['admin-ui/src/App.tsx', 'backend/app/main.py'])).toBe(false);
  });

  it('returns true when models.py changed and no new alembic migration is present', () => {
    expect(needsMigrationGeneration(['backend/app/models.py', 'backend/app/main.py'])).toBe(true);
  });

  it('returns false when models.py changed and a new migration file is present', () => {
    expect(needsMigrationGeneration([
      'backend/app/models.py',
      'backend/alembic/versions/0036_add_thing.py',
    ])).toBe(false);
  });

  it('returns true when only a non-.py file under alembic/versions/ exists (e.g. README)', () => {
    expect(needsMigrationGeneration([
      'backend/app/models.py',
      'backend/alembic/versions/README.md',
    ])).toBe(true);
  });

  it('returns false for an empty changed paths list', () => {
    expect(needsMigrationGeneration([])).toBe(false);
  });
});

// ── runCodeChangeTask marker-detection paths ─────────────────────────────────
// These tests exercise the wrapper itself, not the standard handlers. Verify
// that the three terminal paths (./done marker / PR-URL marker / no marker)
// produce the right persistAndPostStatus calls.
//
// The default mock at the top of this file replaces runCodeChangeTask with a
// no-op so handler tests don't actually exercise the wrapper. For these
// marker tests we need the real implementation, so we import it directly
// from the module's path bypassing the mock via `vi.importActual`.
describe('runCodeChangeTask marker detection', () => {
  let realRunCodeChangeTask: typeof runCodeChangeTask;

  beforeEach(async () => {
    vi.clearAllMocks();
    const real = await vi.importActual<typeof import('./code-change.js')>('./code-change.js');
    realRunCodeChangeTask = real.runCodeChangeTask;
  });

  function baseOpts(taskId = TASK_ID): Parameters<typeof runCodeChangeTask>[0] {
    return {
      taskId,
      prompt: 'do work',
      ws: null,
      statusRef: { value: '' },
      workBranch: 'task-abc123',
      baseBranch: 'main',
      owner: OWNER,
      repo: REPO,
      testsOnMainBranch: true,
      baseCommit: 'basecommit',
    };
  }

  it('exits cleanly without posting when /tmp/done_<id> marker is present', async () => {
    const fs = (await import('fs')).default;
    const idx = await import('../index.js');
    // Stale-marker cleanup unlinks first; then after Claude runs the wrapper
    // calls existsSync on each marker path. Returning true only for the done
    // marker simulates Claude having written it during the session.
    vi.mocked(fs.existsSync).mockImplementation((p: unknown) => p === `/tmp/done_${TASK_ID}`);
    await realRunCodeChangeTask(baseOpts());
    // No status posted — ./done already did that on the agent side.
    expect(vi.mocked(idx.persistAndPostStatus)).not.toHaveBeenCalled();
  });

  it('exits cleanly without posting when /tmp/fail_<id> marker is present', async () => {
    const fs = (await import('fs')).default;
    const idx = await import('../index.js');
    vi.mocked(fs.existsSync).mockImplementation((p: unknown) => p === `/tmp/fail_${TASK_ID}`);
    await realRunCodeChangeTask(baseOpts());
    expect(vi.mocked(idx.persistAndPostStatus)).not.toHaveBeenCalled();
  });

  it('posts COMPLETED with PR URL and changed_files when only /tmp/pr_url_<id> is present', async () => {
    const fs = (await import('fs')).default;
    const idx = await import('../index.js');
    const prUrl = 'https://github.com/owner/repo/pull/77';
    vi.mocked(fs.existsSync).mockImplementation((p: unknown) => p === `/tmp/pr_url_${TASK_ID}`);
    vi.mocked(fs.readFileSync).mockReturnValue(prUrl);
    // git diff --name-only basecommit..HEAD → simulate two changed files.
    vi.mocked(idx.git).mockImplementation((args: readonly string[]) => {
      if (args[0] === 'diff' && args[1] === '--name-only') return 'src/foo.ts\nsrc/bar.ts';
      return '';
    });
    await realRunCodeChangeTask(baseOpts());
    expect(vi.mocked(idx.persistAndPostStatus)).toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({
        status: 'COMPLETED',
        artifact_type: 'PULL_REQUEST',
        pr_url: prUrl,
        changed_files: ['src/foo.ts', 'src/bar.ts'],
      }),
    );
  });

  it('posts FAILED with failure_category="incomplete" when no markers are present and Claude exited 0', async () => {
    const fs = (await import('fs')).default;
    const idx = await import('../index.js');
    vi.mocked(fs.existsSync).mockReturnValue(false);
    // No diff, no commits — wrapper falls through to the FAILED-incomplete
    // path with failure_category: 'incomplete'.
    vi.mocked(idx.git).mockImplementation((args: readonly string[]) => {
      if (args[0] === 'status') return '';
      if (args[0] === 'rev-parse') return 'basecommit';
      return '';
    });
    vi.mocked(idx.runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    await realRunCodeChangeTask(baseOpts());
    expect(vi.mocked(idx.persistAndPostStatus)).toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({
        status: 'FAILED',
        artifact_type: 'ERROR_LOG',
        failure_category: 'incomplete',
      }),
    );
  });

  it('posts FAILED with failure_category="claude_code_failed" when no markers are present and Claude exited non-zero', async () => {
    const fs = (await import('fs')).default;
    const idx = await import('../index.js');
    vi.mocked(fs.existsSync).mockReturnValue(false);
    vi.mocked(idx.git).mockImplementation((args: readonly string[]) => {
      if (args[0] === 'status') return '';
      if (args[0] === 'rev-parse') return 'basecommit';
      return '';
    });
    vi.mocked(idx.runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: 'boom', lastResultText: '' });
    await realRunCodeChangeTask(baseOpts());
    expect(vi.mocked(idx.persistAndPostStatus)).toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({
        status: 'FAILED',
        failure_category: 'claude_code_failed',
      }),
    );
  });

  it('posts COMPLETED with the no-changes message when Claude exits cleanly with no diff and noChangesMessage is set', async () => {
    const fs = (await import('fs')).default;
    const idx = await import('../index.js');
    vi.mocked(fs.existsSync).mockReturnValue(false);
    vi.mocked(idx.git).mockImplementation((args: readonly string[]) => {
      if (args[0] === 'status') return '';
      if (args[0] === 'rev-parse') return 'basecommit';
      return '';
    });
    vi.mocked(idx.runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    const opts = { ...baseOpts(), noChangesMessage: 'No documentation changes needed.' };
    await realRunCodeChangeTask(opts);
    expect(vi.mocked(idx.persistAndPostStatus)).toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({
        status: 'COMPLETED',
        logs: 'No documentation changes needed.',
      }),
    );
  });

  it('cleans up stale markers from a prior attempt before running Claude', async () => {
    const fs = (await import('fs')).default;
    // existsSync false everywhere; we just want to verify unlinkSync is
    // called on each of the three marker paths before runClaudeCode runs.
    vi.mocked(fs.existsSync).mockReturnValue(false);
    const idx = await import('../index.js');
    vi.mocked(idx.runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    await realRunCodeChangeTask(baseOpts());
    const unlinkPaths = vi.mocked(fs.unlinkSync).mock.calls.map(c => c[0]);
    expect(unlinkPaths).toContain(`/tmp/done_${TASK_ID}`);
    expect(unlinkPaths).toContain(`/tmp/fail_${TASK_ID}`);
    expect(unlinkPaths).toContain(`/tmp/pr_url_${TASK_ID}`);
  });
});

// ── runFreshReviewerPass ──────────────────────────────────────────────────────
describe('runFreshReviewerPass', () => {
  const BIG_DIFF = Array.from({ length: 25 }, (_, i) => `+line ${i}`).join('\n');

  beforeEach(() => { vi.clearAllMocks(); });

  it('skips reviewer pass when diff is below minimum line threshold', async () => {
    const idx = await import('../index.js');
    vi.mocked(idx.git).mockReturnValue('+one\n-one'); // only 2 changed lines
    await runFreshReviewerPass('t1', 'Title', 'Desc', [], null, { value: '' });
    expect(vi.mocked(idx.runClaudeCode)).not.toHaveBeenCalled();
  });

  it('runs reviewer pass and merges findings into self-review JSON', async () => {
    const idx = await import('../index.js');
    vi.mocked(idx.git).mockReturnValue(BIG_DIFF);
    vi.mocked(idx.runClaudeCode).mockResolvedValue({
      exitCode: 0, allOutput: '',
      lastResultText: '{"reviewer_findings": [{"category": "correctness", "severity": "HIGH", "description": "off-by-one"}]}',
    });
    const { default: fs } = await import('fs');
    vi.mocked(fs.readFileSync).mockImplementation((p: unknown) => {
      if (typeof p === 'string' && p.includes('self_review_t2')) {
        return '{"verdict":"pass","check_results":{"correctness":{"passed":true}}}';
      }
      return '';
    });
    await runFreshReviewerPass('t2', 'Title', 'Desc', ['src/foo.ts'], null, { value: '' });
    const writeCalls = vi.mocked(fs.writeFileSync).mock.calls;
    const srWrite = writeCalls.find(c => (c[0] as string).includes('self_review_t2'));
    expect(srWrite).toBeDefined();
    const srContent = srWrite?.[1] as string | undefined;
    expect(srContent).toBeDefined();
    const written = JSON.parse(srContent ?? '{}') as Record<string, unknown>;
    expect((written.reviewer_findings as unknown[]).length).toBe(1);
    const checkResults = written.check_results as Record<string, { passed: boolean }>;
    expect(checkResults.correctness.passed).toBe(false);
  });

  it('falls back gracefully when reviewer session exits non-zero', async () => {
    const idx = await import('../index.js');
    vi.mocked(idx.git).mockReturnValue(BIG_DIFF);
    vi.mocked(idx.runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: '', lastResultText: '' });
    const { default: fs } = await import('fs');
    await runFreshReviewerPass('t3', 'Title', 'Desc', [], null, { value: '' });
    expect(vi.mocked(fs.writeFileSync)).not.toHaveBeenCalledWith(
      expect.stringContaining('self_review_t3'), expect.anything(), expect.anything(),
    );
  });

  it('falls back gracefully when reviewer output is unparseable', async () => {
    const idx = await import('../index.js');
    vi.mocked(idx.git).mockReturnValue(BIG_DIFF);
    vi.mocked(idx.runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: 'not json' });
    const { default: fs } = await import('fs');
    await runFreshReviewerPass('t4', 'Title', 'Desc', [], null, { value: '' });
    expect(vi.mocked(fs.writeFileSync)).not.toHaveBeenCalledWith(
      expect.stringContaining('self_review_t4'), expect.anything(), expect.anything(),
    );
  });

  it('keeps the reviewer prompt unchanged when calibration data is absent or empty', async () => {
    const idx = await import('../index.js');
    vi.mocked(idx.git).mockReturnValue(BIG_DIFF);
    vi.mocked(idx.runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '{"reviewer_findings":[]}' });

    await runFreshReviewerPass('t5', 'Title', 'Desc', [], null, { value: '' });
    const promptWithoutCalibration = vi.mocked(idx.runClaudeCode).mock.calls.at(-1)?.[0];
    if (typeof promptWithoutCalibration !== 'string') throw new Error('expected reviewer prompt');

    vi.mocked(idx.runClaudeCode).mockClear();
    await runFreshReviewerPass('t6', 'Title', 'Desc', [], null, { value: '' }, undefined, { self_review_catch_rate: { categories: {} } });
    const promptWithEmptyCalibration = vi.mocked(idx.runClaudeCode).mock.calls.at(-1)?.[0];
    if (typeof promptWithEmptyCalibration !== 'string') throw new Error('expected reviewer prompt');

    expect(promptWithEmptyCalibration).toBe(promptWithoutCalibration);
    expect(promptWithEmptyCalibration).not.toContain('Calibration from prior self-review misses:');
  });

  it('adds a ranked calibration block with top miss categories and examples', async () => {
    const idx = await import('../index.js');
    vi.mocked(idx.git).mockReturnValue(BIG_DIFF);
    vi.mocked(idx.runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '{"reviewer_findings":[]}' });

    await runFreshReviewerPass(
      't7',
      'Title',
      'Desc',
      ['src/foo.ts'],
      null,
      { value: '' },
      undefined,
      {
        self_review_catch_rate: {
          categories: {
            security: {
              miss_rate: 0.6,
              weighted_miss_rate: 0.9,
              examples: [
                { severity: 'HIGH', desc: 'missing auth check' },
                { severity: 'LOW', desc: 'unsanitized input' },
                { severity: 'LOW', desc: 'extra example that should be omitted' },
              ],
            },
            correctness: {
              miss_rate: 0.4,
              weighted_miss_rate: 0.6,
              examples: [{ severity: 'MEDIUM', desc: 'off-by-one loop' }],
            },
            error_handling: {
              miss_rate: 0.2,
              weighted_miss_rate: 0.3,
              examples: [{ severity: 'MEDIUM', desc: 'missing null guard' }],
            },
            type_safety: {
              miss_rate: 0.1,
              weighted_miss_rate: 0.2,
              examples: [{ severity: 'LOW', desc: 'unsafe cast' }],
            },
          },
        },
      },
    );

    const prompt = vi.mocked(idx.runClaudeCode).mock.calls.at(-1)?.[0];
    if (typeof prompt !== 'string') throw new Error('expected reviewer prompt');
    const calibrationIndex = prompt.indexOf('Calibration from prior self-review misses:');
    expect(calibrationIndex).toBeGreaterThanOrEqual(0);
    expect(prompt).toContain('- security: 60% missed (90% severity-weighted)');
    expect(prompt).toContain('  - [HIGH] missing auth check');
    expect(prompt).toContain('  - [LOW] unsanitized input');
    expect(prompt).not.toContain('extra example that should be omitted');
    expect(prompt.indexOf('- security:')).toBeLessThan(prompt.indexOf('- correctness:'));
    expect(prompt.indexOf('- correctness:')).toBeLessThan(prompt.indexOf('- error_handling:'));
    expect(prompt).not.toContain('- type_safety:');
    expect(prompt).toContain('Scrutinize these categories extra carefully.');
  });
});

// ── enable_fresh_reviewer flag integration ────────────────────────────────────
describe('handleCreate — enable_fresh_reviewer flag', () => {
  const BIG_DIFF = Array.from({ length: 25 }, (_, i) => `+line ${i}`).join('\n');

  beforeEach(() => { vi.clearAllMocks(); });

  it('runs fresh-reviewer pass when project_metadata.enable_fresh_reviewer is true', async () => {
    const idx = await import('../index.js');
    vi.mocked(idx.git).mockReturnValue(BIG_DIFF);
    vi.mocked(idx.runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '{"reviewer_findings":[]}' });
    vi.mocked(idx.setupWorkspace).mockResolvedValue({ workBranch: 'task-abc123', shortId: 'abc123' });

    const task = {
      title: 'Test task', description: '',
      context: {}, project_context: {},
      project_metadata: { enable_fresh_reviewer: true },
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const reviewerCall = vi.mocked(idx.runClaudeCode).mock.calls.find(
      call => typeof call[0] === 'string' && call[0].includes('You are reviewing a PR'),
    );
    expect(reviewerCall).toBeDefined();
  });

  it('skips fresh-reviewer pass when enable_fresh_reviewer is false', async () => {
    const idx = await import('../index.js');
    vi.mocked(idx.git).mockReturnValue(BIG_DIFF);
    vi.mocked(idx.runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    vi.mocked(idx.setupWorkspace).mockResolvedValue({ workBranch: 'task-abc123', shortId: 'abc123' });

    const task = {
      title: 'Test task', description: '',
      context: {}, project_context: {},
      project_metadata: { enable_fresh_reviewer: false },
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const reviewerCall = vi.mocked(idx.runClaudeCode).mock.calls.find(
      call => typeof call[0] === 'string' && call[0].includes('You are reviewing a PR'),
    );
    expect(reviewerCall).toBeUndefined();
  });

  it('runs fresh-reviewer pass when project_metadata.is_self_repo is true and enable_fresh_reviewer is unset', async () => {
    const idx = await import('../index.js');
    vi.mocked(idx.git).mockReturnValue(BIG_DIFF);
    vi.mocked(idx.runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '{"reviewer_findings":[]}' });
    vi.mocked(idx.setupWorkspace).mockResolvedValue({ workBranch: 'task-abc123', shortId: 'abc123' });

    const task = {
      title: 'Test task', description: '',
      context: {}, project_context: {},
      project_metadata: { is_self_repo: true },
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const reviewerCall = vi.mocked(idx.runClaudeCode).mock.calls.find(
      call => typeof call[0] === 'string' && call[0].includes('You are reviewing a PR'),
    );
    expect(reviewerCall).toBeDefined();
  });

  it('still skips fresh-reviewer pass for auto-remediation tasks even when is_self_repo is true', async () => {
    const idx = await import('../index.js');
    vi.mocked(idx.git).mockReturnValue(BIG_DIFF);
    vi.mocked(idx.runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    vi.mocked(idx.setupWorkspace).mockResolvedValue({ workBranch: 'task-abc123', shortId: 'abc123' });

    const task = {
      title: 'Test task', description: '',
      context: { auto_remediation: true }, project_context: {},
      project_metadata: { is_self_repo: true, enable_fresh_reviewer: false },
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const reviewerCall = vi.mocked(idx.runClaudeCode).mock.calls.find(
      call => typeof call[0] === 'string' && call[0].includes('You are reviewing a PR'),
    );
    expect(reviewerCall).toBeUndefined();
  });

  it('skips fresh-reviewer pass even when flag is true for auto-remediation tasks', async () => {
    const idx = await import('../index.js');
    vi.mocked(idx.git).mockReturnValue(BIG_DIFF);
    vi.mocked(idx.runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    vi.mocked(idx.setupWorkspace).mockResolvedValue({ workBranch: 'task-abc123', shortId: 'abc123' });

    const task = {
      title: 'Test task', description: '',
      context: { auto_remediation: true }, project_context: {},
      project_metadata: { enable_fresh_reviewer: true },
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const reviewerCall = vi.mocked(idx.runClaudeCode).mock.calls.find(
      call => typeof call[0] === 'string' && call[0].includes('You are reviewing a PR'),
    );
    expect(reviewerCall).toBeUndefined();
  });
});

// ── Fresh-reviewer merge logic (override semantics) ───────────────────────────
// Tests the core merge behavior: how reviewer findings override or preserve
// existing self-review check results.
describe('runFreshReviewerPass merge logic', () => {
  const BIG_DIFF = Array.from({ length: 25 }, (_, i) => `+line ${i}`).join('\n');
  let fsModule: typeof import('fs');

  beforeEach(async () => {
    vi.clearAllMocks();
    fsModule = await import('fs');
    // Reset writeFileSync captures
    vi.mocked(fsModule.writeFileSync).mockClear();
    vi.mocked(fsModule.readFileSync).mockClear();
  });

  it('creates new self-review JSON with only reviewer_findings when JSON is missing', async () => {
    const idx = await import('../index.js');
    vi.mocked(fsModule.readFileSync).mockImplementation(() => {
      throw new Error('File not found');
    });
    let capturedJson: Record<string, unknown> | null = null;
    vi.mocked(fsModule.writeFileSync).mockImplementation((_path: unknown, data: unknown) => {
      capturedJson = JSON.parse((data as string | Buffer).toString()) as Record<string, unknown>;
    });
    vi.mocked(idx.git).mockReturnValue(BIG_DIFF);
    vi.mocked(idx.runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: '{"reviewer_findings":[{"category":"correctness","severity":"HIGH","description":"Logic bug"}]}',
    });

    const taskId = 'test-merge-1';
    await runFreshReviewerPass(taskId, 'Test task', 'Test description', [], null, { value: '' });

    expect(capturedJson).not.toBeNull();
    const json = capturedJson as unknown as Record<string, unknown>;
    expect(json.reviewer_findings).toBeDefined();
    expect((json.reviewer_findings as Array<unknown>).length).toBe(1);
    expect((json.reviewer_findings as Array<unknown>)[0]).toEqual({
      category: 'correctness',
      severity: 'HIGH',
      description: 'Logic bug',
    });
  });

  it('adds reviewer-flagged category absent from check_results with passed=false', async () => {
    const idx = await import('../index.js');
    vi.mocked(fsModule.readFileSync).mockReturnValue(JSON.stringify({
      verdict: 'pass',
      check_results: { 'type-safety': { passed: true, issue: '' } },
    }));
    let capturedJson: Record<string, unknown> | null = null;
    vi.mocked(fsModule.writeFileSync).mockImplementation((_path: unknown, data: unknown) => {
      capturedJson = JSON.parse((data as string | Buffer).toString()) as Record<string, unknown>;
    });
    vi.mocked(idx.git).mockReturnValue(BIG_DIFF);
    vi.mocked(idx.runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: '{"reviewer_findings":[{"category":"correctness","severity":"HIGH","description":"Off-by-one error"}]}',
    });

    const taskId = 'test-merge-2';
    await runFreshReviewerPass(taskId, 'Test task', 'Test description', [], null, { value: '' });

    expect(capturedJson).not.toBeNull();
    const json = capturedJson as unknown as Record<string, unknown>;
    const checkResults = json.check_results as Record<string, Record<string, unknown>>;
    expect(checkResults.correctness).toBeDefined();
    expect(checkResults.correctness.passed).toBe(false);
    expect((checkResults.correctness.issue as string)).toContain('[reviewer]');
    expect((checkResults.correctness.issue as string)).toContain('Off-by-one error');
  });

  it('overrides reviewer-flagged category with passed=true to passed=false', async () => {
    const idx = await import('../index.js');
    vi.mocked(fsModule.readFileSync).mockReturnValue(JSON.stringify({
      verdict: 'pass',
      check_results: {
        correctness: { passed: true, issue: '' },
        'error-handling': { passed: true, issue: '' },
      },
    }));
    let capturedJson: Record<string, unknown> | null = null;
    vi.mocked(fsModule.writeFileSync).mockImplementation((_path: unknown, data: unknown) => {
      capturedJson = JSON.parse((data as string | Buffer).toString()) as Record<string, unknown>;
    });
    vi.mocked(idx.git).mockReturnValue(BIG_DIFF);
    vi.mocked(idx.runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: '{"reviewer_findings":[{"category":"correctness","severity":"HIGH","description":"Missing null check"}]}',
    });

    const taskId = 'test-merge-3';
    await runFreshReviewerPass(taskId, 'Test task', 'Test description', [], null, { value: '' });

    expect(capturedJson).not.toBeNull();
    const json = capturedJson as unknown as Record<string, unknown>;
    const checkResults = json.check_results as Record<string, Record<string, unknown>>;
    expect(checkResults.correctness.passed).toBe(false);
    expect((checkResults.correctness.issue as string)).toContain('[reviewer]');
    // error-handling should remain unchanged from original
    expect(checkResults['error-handling'].passed).toBe(true);
  });

  it('does NOT override reviewer-flagged category with passed=false (preserves issue text)', async () => {
    const idx = await import('../index.js');
    const originalIssue = 'Original reviewer caught this first';
    vi.mocked(fsModule.readFileSync).mockReturnValue(JSON.stringify({
      verdict: 'pass',
      check_results: {
        correctness: { passed: false, issue: originalIssue },
      },
    }));
    let capturedJson: Record<string, unknown> | null = null;
    vi.mocked(fsModule.writeFileSync).mockImplementation((_path: unknown, data: unknown) => {
      capturedJson = JSON.parse((data as string | Buffer).toString()) as Record<string, unknown>;
    });
    vi.mocked(idx.git).mockReturnValue(BIG_DIFF);
    vi.mocked(idx.runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: '{"reviewer_findings":[{"category":"correctness","severity":"CRITICAL","description":"Different issue"}]}',
    });

    const taskId = 'test-merge-4';
    await runFreshReviewerPass(taskId, 'Test task', 'Test description', [], null, { value: '' });

    expect(capturedJson).not.toBeNull();
    const json = capturedJson as unknown as Record<string, unknown>;
    const checkResults = json.check_results as Record<string, Record<string, unknown>>;
    expect(checkResults.correctness.passed).toBe(false);
    // Original issue text should be preserved, not overwritten by new reviewer finding
    expect(checkResults.correctness.issue).toBe(originalIssue);
  });

  it('always writes reviewer_findings array to self-review JSON', async () => {
    const idx = await import('../index.js');
    vi.mocked(fsModule.readFileSync).mockReturnValue(JSON.stringify({
      verdict: 'pass',
      check_results: { 'type-safety': { passed: true, issue: '' } },
    }));
    let capturedJson: Record<string, unknown> | null = null;
    vi.mocked(fsModule.writeFileSync).mockImplementation((_path: unknown, data: unknown) => {
      capturedJson = JSON.parse((data as string | Buffer).toString()) as Record<string, unknown>;
    });
    vi.mocked(idx.git).mockReturnValue(BIG_DIFF);
    const findings = [
      { category: 'correctness', severity: 'HIGH', description: 'Logic error' },
      { category: 'scope-drift', severity: 'MEDIUM', description: 'Changed unrelated file' },
    ];
    vi.mocked(idx.runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: JSON.stringify({ reviewer_findings: findings }),
    });

    const taskId = 'test-merge-5';
    await runFreshReviewerPass(taskId, 'Test task', 'Test description', [], null, { value: '' });

    expect(capturedJson).not.toBeNull();
    const json = capturedJson as unknown as Record<string, unknown>;
    expect(json.reviewer_findings).toEqual(findings);
  });
});

// ── Structural prompt invariants (regression-detection) ───────────────────────
// Pins the cross-cutting guidance blocks that every code-change handler MUST
// assemble into its prompt. A future PR that drops one of these constants from
// runStandardHandler's finalPrompt assembly will fail here before merging.
describe('code-change handler structural prompt invariants', () => {
  const TASK = { title: 'T', description: '', context: {}, project_context: {} };
  const PAYLOAD = { prompt: 'do something' };

  beforeEach(() => { vi.clearAllMocks(); });

  it('handleCreate prompt includes all required code-change guidance blocks', async () => {
    await handleCreate(TASK_ID, PAYLOAD, TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const p = capturedPrompt();
    expect(p).toContain('Task lifecycle — you own it end-to-end');       // CODE_CHANGE_HELPER_GUIDANCE
    expect(p).toContain('./playbook set');                                // PLAYBOOK_GUIDANCE
    expect(p).toContain('Read The Failure Message Literally');            // READ_THE_FAILURE_MESSAGE
    expect(p).toContain('Size Guardrails Are Advisory');                  // SIZE_GUARDRAIL_GUIDANCE
    expect(p).toContain("If You're Retrying");                           // PRIOR_ATTEMPT_DIVERGENCE
    expect(p).toContain('When You Find Bigger Problems Than Your Task');  // BEYOND_SCOPE_REPORTING
    expect(p).toContain('Use Grep/Glob for code search');                // DEFAULT_TOOL_GUIDANCE
  });

  it('handleRefactor prompt includes required code-change guidance blocks', async () => {
    await handleRefactor(TASK_ID, PAYLOAD, TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const p = capturedPrompt();
    expect(p).toContain('Task lifecycle — you own it end-to-end');
    expect(p).toContain('Use Grep/Glob for code search');
  });

  it('handleDocs prompt includes required code-change guidance blocks', async () => {
    await handleDocs(TASK_ID, PAYLOAD, TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const p = capturedPrompt();
    expect(p).toContain('Task lifecycle — you own it end-to-end');
    expect(p).toContain('Use Grep/Glob for code search');
  });

  it('handleQA prompt includes required code-change guidance blocks', async () => {
    await handleQA(TASK_ID, PAYLOAD, TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const p = capturedPrompt();
    expect(p).toContain('Task lifecycle — you own it end-to-end');
    expect(p).toContain('Use Grep/Glob for code search');
  });

  it('handleDeps prompt includes required code-change guidance blocks', async () => {
    await handleDeps(TASK_ID, PAYLOAD, TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const p = capturedPrompt();
    expect(p).toContain('Task lifecycle — you own it end-to-end');
    expect(p).toContain('Use Grep/Glob for code search');
  });
});

// ── Task-type-specific self-review focus blocks ───────────────────────────────
describe('task-type self-review focus blocks', () => {
  const TASK = { title: 'T', description: '', context: {}, project_context: {} };

  beforeEach(() => { vi.clearAllMocks(); });

  it('includes migrate focus block and generic guidance for migrate task type', async () => {
    await handleCreate(TASK_ID, { prompt: 'do something', type: 'migrate' }, TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const p = capturedPrompt();
    expect(p).toContain('Self-review focus: migrate tasks');
    expect(p).toContain('Task lifecycle — you own it end-to-end');
  });

  it('includes refactor focus block and generic guidance for refactor task type', async () => {
    await handleRefactor(TASK_ID, { prompt: 'do something', type: 'refactor' }, TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const p = capturedPrompt();
    expect(p).toContain('Self-review focus: refactor tasks');
    expect(p).toContain('Task lifecycle — you own it end-to-end');
  });

  it('includes qa focus block and generic guidance for qa task type', async () => {
    await handleQA(TASK_ID, { prompt: 'do something', type: 'qa' }, TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const p = capturedPrompt();
    expect(p).toContain('Self-review focus: qa tasks');
    expect(p).toContain('Task lifecycle — you own it end-to-end');
  });

  it('does not include any focus block for create task type', async () => {
    await handleCreate(TASK_ID, { prompt: 'do something', type: 'create' }, TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).not.toContain('Self-review focus:');
  });

  it('does not include any focus block when task type is absent', async () => {
    await handleCreate(TASK_ID, { prompt: 'do something' }, TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).not.toContain('Self-review focus:');
  });
});
