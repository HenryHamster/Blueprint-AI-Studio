import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── Module mocks ──────────────────────────────────────────────────────────────
vi.mock('../index.js', () => ({
  WORKSPACE: '/tmp/workspace',
  strVal: (v: unknown, fallback = '') => (typeof v === 'string' ? v : fallback),
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
  pushAndCreatePR: vi.fn().mockResolvedValue('https://github.com/owner/repo/pull/1'),
  runClaudeCode: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' }),
  matchesRateLimit: vi.fn().mockReturnValue(false),
  lintFixLoop: vi.fn().mockResolvedValue(true),
  testFixLoop: vi.fn().mockResolvedValue({ testFailuresDetected: false, passed: true }),
  captureTestBaseline: vi.fn().mockResolvedValue(null),
  condensedAnalysisSections: vi.fn((s: string) => s),
  runProjectLintChecks: vi.fn(),
  cloneWithRetry: vi.fn(),
  sendHeartbeatSync: vi.fn(),
  getProviderRunner: vi.fn().mockReturnValue({ displayName: 'Claude Code' }),
}));

vi.mock('../prompt.js', () => ({
  TASK_TYPE_HINTS: {},
  getTaskTypeHints: vi.fn().mockReturnValue({}),
  DEFAULT_TOOL_GUIDANCE: '',
  PLAYBOOK_GUIDANCE: '',
  readClaudeMd: vi.fn().mockReturnValue(''),
  buildContextPrefix: vi.fn().mockReturnValue(''),
  buildFileTree: vi.fn().mockReturnValue(''),
  discoverRelatedTests: vi.fn().mockReturnValue(''),
  discoverModuleInterfaces: vi.fn().mockReturnValue(''),
  configureAgentPermissions: vi.fn().mockReturnValue([]),
  writePlaybookHelper: vi.fn(),
  writeMessagesHelper: vi.fn(),
  formatPlaybook: vi.fn().mockReturnValue(''),
  buildRetryContext: vi.fn().mockReturnValue(''),
  buildSelfReviewPrompt: vi.fn().mockReturnValue('self-review-prompt'),
  buildAnalysisPrompt: vi.fn().mockReturnValue('analysis-prompt'),
  parseAnalysisPlanFiles: vi.fn().mockReturnValue([]),
  parseAnalysisPlanTestFiles: vi.fn().mockReturnValue([]),
  parseAnalysisPlanVerifyFiles: vi.fn().mockReturnValue([]),
  extractModuleInterfaces: vi.fn().mockReturnValue(''),
  discoverAffectedConsumers: vi.fn().mockReturnValue(''),
  getMandatoryChecks: vi.fn().mockReturnValue([]),
}));

vi.mock('../build-config.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../build-config.js')>();
  return { ...actual };
});

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
import { handleCreate, handleRefactor, handleDocs, handleQA, handleDeps, extractSelfReviewJson, needsMigrationGeneration, buildSmallScopeCallerNote } from './standard.js';
import { setupWorkspace, runClaudeCode, pushAndCreatePR, testFixLoop, captureTestBaseline, git, lintFixLoop, persistAndPostStatus, runProjectLintChecks, sendHeartbeatSync } from '../index.js';
import { extractModuleInterfaces, discoverAffectedConsumers, getMandatoryChecks, parseAnalysisPlanFiles, parseAnalysisPlanTestFiles, parseAnalysisPlanVerifyFiles, buildAnalysisPrompt } from '../prompt.js';

// ── Shared test helpers ───────────────────────────────────────────────────────
const TASK_ID = 'task-123';
const GH_TOKEN = 'gh-token';
const OWNER = 'owner';
const REPO = 'repo';
const WS = null;
const STATUS_REF = () => ({ value: '' });

/** Returns the first argument passed to runClaudeCode (the full prompt string). */
function capturedPrompt(): string {
  return vi.mocked(runClaudeCode).mock.calls[0][0] as string;
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

// ── Handler configuration: branchPrefix and prompt content ───────────────────
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

  it('includes Closes #N in PR body when task.context.issue_number is set', async () => {
    const { git } = await import('../index.js');
    vi.mocked(git).mockReturnValueOnce('M src/foo.ts'); // baseCommit (makes currentHead !== baseCommit → hasChanges)
    const validJson = '{"verdict":"PASS","checks_performed":[],"issues_found":0}';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })    // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' }); // self-review pass1
    const task = { title: 'My Task', description: '', context: { issue_number: 42 } };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prBodyArg = vi.mocked(pushAndCreatePR).mock.calls[0][6] as string;
    expect(prBodyArg).toContain('Closes #42');
  });

  it('uses issue_url over issue_number in PR body when both are set', async () => {
    const { git } = await import('../index.js');
    vi.mocked(git).mockReturnValueOnce('M src/foo.ts'); // baseCommit (makes currentHead !== baseCommit → hasChanges)
    const validJson = '{"verdict":"PASS","checks_performed":[],"issues_found":0}';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })    // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' }); // self-review pass1
    const task = { title: 'My Task', description: '', context: { issue_number: 42, issue_url: 'https://github.com/owner/repo/issues/42' } };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prBodyArg = vi.mocked(pushAndCreatePR).mock.calls[0][6] as string;
    expect(prBodyArg).toContain('Closes https://github.com/owner/repo/issues/42');
  });

  it('does not include Closes in PR body when no issue context', async () => {
    const { git } = await import('../index.js');
    vi.mocked(git).mockReturnValueOnce('M src/foo.ts'); // baseCommit (makes currentHead !== baseCommit → hasChanges)
    const validJson = '{"verdict":"PASS","checks_performed":[],"issues_found":0}';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })    // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' }); // self-review pass1
    const task = { title: 'My Task', description: '' };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prBodyArg = vi.mocked(pushAndCreatePR).mock.calls[0][6] as string;
    expect(prBodyArg).not.toContain('Closes');
  });

  it('includes direction title and id in PR footer when both are set', async () => {
    const { git } = await import('../index.js');
    vi.mocked(git).mockReturnValueOnce('M src/foo.ts');
    const validJson = '{"verdict":"PASS","checks_performed":[],"issues_found":0}';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' });
    const task = { title: 'My Task', description: '', direction_context: { title: 'PR clarity' }, context: { direction_id: 'd-abc12345' } };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prBodyArg = vi.mocked(pushAndCreatePR).mock.calls[0][6] as string;
    expect(prBodyArg).toContain('direction: PR clarity (`d-abc12345`)');
  });

  it('includes direction id alone in PR footer when title is absent', async () => {
    const { git } = await import('../index.js');
    vi.mocked(git).mockReturnValueOnce('M src/foo.ts');
    const validJson = '{"verdict":"PASS","checks_performed":[],"issues_found":0}';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' });
    const task = { title: 'My Task', description: '', context: { direction_id: 'd-abc12345' } };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prBodyArg = vi.mocked(pushAndCreatePR).mock.calls[0][6] as string;
    expect(prBodyArg).toContain('direction:');
    expect(prBodyArg).toContain('d-abc12345');
  });

  it('omits direction from PR footer when no direction context is set', async () => {
    const { git } = await import('../index.js');
    vi.mocked(git).mockReturnValueOnce('M src/foo.ts');
    const validJson = '{"verdict":"PASS","checks_performed":[],"issues_found":0}';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' });
    const task = { title: 'My Task', description: '' };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prBodyArg = vi.mocked(pushAndCreatePR).mock.calls[0][6] as string;
    expect(prBodyArg).not.toContain('direction:');
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
    expect(capturedPrompt()).toContain('REFACTOR TASK \u2014 STRICT CONSTRAINTS');
  });

  it('marks task FAILED when test failures persist (failOnTestFailure gate)', async () => {
    vi.mocked(git)
      .mockReturnValueOnce('')        // rev-parse HEAD → baseCommit
      .mockReturnValueOnce('M file')  // status --porcelain → hasChanges = true
      .mockReturnValue('');           // all remaining calls
    vi.mocked(testFixLoop).mockResolvedValueOnce({ testFailuresDetected: true, passed: false, failureSummary: 'some test failed' });
    const task = { title: 'Refactor task', description: '', context: { scope: 'small' } };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(persistAndPostStatus)).toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({ status: 'FAILED', failure_category: 'test_failed' }),
    );
    expect(vi.mocked(pushAndCreatePR)).not.toHaveBeenCalled();
  });

  it('captures test baseline for medium-scope refactor tasks', async () => {
    const task = { title: 'Refactor task', description: '', context: { scope: 'medium' } };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).toHaveBeenCalled();
  });

  it('captures test baseline for small-scope refactor tasks', async () => {
    const task = { title: 'Refactor task', description: '', context: { scope: 'small' } };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).toHaveBeenCalled();
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
    // For medium-scope tasks, call[0] is the analysis phase; call[1] is the main task prompt
    const mainPrompt = vi.mocked(runClaudeCode).mock.calls[1][0] as string;
    expect(mainPrompt).toContain('## Module Interfaces');
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
    // For medium-scope tasks, call[0] is the analysis phase; call[1] is the main task prompt
    const mainPrompt = vi.mocked(runClaudeCode).mock.calls[1][0] as string;
    expect(mainPrompt).toContain('## Module Interfaces');
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
    // For medium-scope tasks, call[0] is the analysis phase; call[1] is the main task prompt
    const mainPrompt = vi.mocked(runClaudeCode).mock.calls[1][0] as string;
    expect(mainPrompt).toContain('## Affected Consumers');
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
    // For medium-scope tasks, call[0] is the analysis phase; call[1] is the main task prompt
    const mainPrompt = vi.mocked(runClaudeCode).mock.calls[1][0] as string;
    expect(mainPrompt).toContain('## Affected Consumers');
  });

  it('does not call discoverAffectedConsumers for handleRefactor with scope=small', async () => {
    const task = { title: 'Refactor', description: '', context: { scope: 'small' } };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(discoverAffectedConsumers)).not.toHaveBeenCalled();
  });
});

// ── Pre-implementation analysis note (small scope only) ───────────────────────
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
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
  });

  it('does not inject Pre-Implementation Analysis for handleCreate with scope=medium', async () => {
    const task = { title: 'Task', description: '', context: { scope: 'medium' } };
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
    // Only the main implementation runClaudeCode call should fire — analysis phase is skipped.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
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

// ── Self-review phase ────────────────────────────────────────────────────────
describe('self-review phase', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getMandatoryChecks).mockReturnValue([]);
  });

  it('runs self-review for handleCreate when there are changes', async () => {
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '{"verdict":"PASS","checks_performed":[],"issues_found":0}', lastResultText: '' }); // self-review pass 1 (no fixes → no pass 2)
    vi.mocked(git)
      .mockReturnValueOnce('')       // rev-parse HEAD → baseCommit
      .mockReturnValueOnce('M file') // status --porcelain → hasChanges = true
      .mockReturnValue('');          // all remaining git calls
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // runClaudeCode should be called twice: once for main task, once for self-review pass 1
    // pass 2 is skipped because pass 1 applied no fixes (headBefore === headAfterPass1)
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
  });

  it('runs self-review for handleRefactor when there are changes', async () => {
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '{"verdict":"PASS","checks_performed":[],"issues_found":0}', lastResultText: '' }); // self-review pass 1
    vi.mocked(git)
      .mockReturnValueOnce('')
      .mockReturnValueOnce('M file')
      .mockReturnValue('');
    const task = { title: 'Refactor task', description: '' };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // refactor: failOnTestFailure=false so validation gate doesn't apply; pass 2 skipped (no fixes)
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
  });

  it('skips self-review when there are no changes', async () => {
    // Default git mock returns '' everywhere → no changes, goes to no-changes branch
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
  });

  it('does not run self-review for handleDocs even when there are changes', async () => {
    vi.mocked(git)
      .mockReturnValueOnce('')
      .mockReturnValueOnce('M file')
      .mockReturnValue('');
    const task = { title: 'Docs task', description: '' };
    await handleDocs(TASK_ID, {}, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // docs handler does not set runSelfReview — only one runClaudeCode call
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
  });

  it('runs self-review for handleQA when there are changes', async () => {
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '{"verdict":"PASS","checks_performed":[],"issues_found":0}', lastResultText: '' }); // self-review pass 1
    vi.mocked(git)
      .mockReturnValueOnce('')       // rev-parse HEAD → baseCommit
      .mockReturnValueOnce('M file') // status --porcelain → hasChanges = true
      .mockReturnValue('');          // all remaining git calls
    const task = { title: 'QA task', description: '' };
    await handleQA(TASK_ID, {}, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // runClaudeCode should be called twice: once for main task, once for self-review pass 1
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
  });

  it('includes self-review result in PR body when fixes were applied', async () => {
    // Simulate: main task diff, then self-review pass 1 changes something (HEAD changes) → pass 2 runs
    const validJson = '{"verdict":"PASS","checks_performed":[],"issues_found":0}';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' })  // pass 1
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' }); // pass 2 (triggered by fixes)
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit (rev-parse HEAD before Claude)
      .mockReturnValueOnce('M file')   // diff (status --porcelain → hasChanges)
      .mockReturnValueOnce('')         // currentHead (same as base)
      .mockReturnValueOnce('')         // uncommittedDiff for self-review (diff HEAD)
      .mockReturnValueOnce('abc123')   // headBeforeReview (rev-parse HEAD)
      .mockReturnValueOnce('')         // statusBeforeReview
      .mockReturnValueOnce('def456')   // headAfterPass1 — different → pass1FixesApplied=true → pass 2
      .mockReturnValueOnce('M fixed')  // statusAfterPass1
      .mockReturnValueOnce('def456')   // headAfterPass2 — same as pass1 → no additional fixes
      .mockReturnValueOnce('M fixed')  // statusAfterPass2
      .mockReturnValue('');
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prBodyArg = vi.mocked(pushAndCreatePR).mock.calls[0][6] as string;
    expect(prBodyArg).toContain('Self-Review');
    expect(prBodyArg).toContain('Issues found and auto-fixed');
  });

  it('includes self-review result in PR body when no issues found', async () => {
    // headBeforeReview === headAfterPass1 → no fixes applied → no pass 2 → PASS verdict
    const validJson = '{"verdict":"PASS","checks_performed":[],"issues_found":0}';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' }); // pass 1 (no fixes → no pass 2)
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit
      .mockReturnValueOnce('M file')   // hasChanges
      .mockReturnValueOnce('')         // currentHead (same as base)
      .mockReturnValueOnce('')         // uncommittedDiff (diff HEAD)
      .mockReturnValueOnce('abc123')   // headBeforeReview
      .mockReturnValueOnce('')         // statusBeforeReview
      .mockReturnValueOnce('abc123')   // headAfterPass1 — SAME → no fixes → no pass 2
      .mockReturnValueOnce('')         // statusAfterPass1 — SAME
      .mockReturnValue('');
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prBodyArg = vi.mocked(pushAndCreatePR).mock.calls[0][6] as string;
    expect(prBodyArg).toContain('Self-Review');
    expect(prBodyArg).toContain('No critical issues found');
  });

  it('sets selfReviewOutcome categories to self_review_failed when runClaudeCode exits non-zero', async () => {
    // First runClaudeCode call (main task) succeeds, second (self-review) fails with exitCode=1
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })
      .mockResolvedValueOnce({ exitCode: 1, allOutput: 'some error', lastResultText: '' });
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit
      .mockReturnValueOnce('M file')   // hasChanges
      .mockReturnValue('');            // remaining
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // self_review_outcome should be reported to backend with categories=['self_review_failed']
    const statusCall = vi.mocked(persistAndPostStatus).mock.calls.find(
      c => (c[1] as Record<string, unknown>).status === 'COMPLETED',
    );
    expect(statusCall).toBeDefined();
    const outcome = (statusCall![1] as Record<string, unknown>).self_review_outcome as { categories: string[]; passes: number };
    expect(outcome).toBeDefined();
    expect(outcome.categories).toContain('self_review_failed');
    expect(outcome.passes).toBe(1);
  });

  it('runs verification pass (pass 2) when pass 1 applied fixes', async () => {
    // main task → pass 1 (fixes applied) → pass 2 (verification) = 3 runClaudeCode calls total
    const validJson = '{"verdict":"PASS","checks_performed":[],"issues_found":0}';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' })  // pass 1
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' }); // pass 2 (verification)
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit (rev-parse HEAD before Claude)
      .mockReturnValueOnce('M file')   // hasChanges (status --porcelain)
      .mockReturnValueOnce('')         // currentHead (same as base)
      .mockReturnValueOnce('')         // uncommittedDiff (diff HEAD)
      .mockReturnValueOnce('abc123')   // headBeforeReview
      .mockReturnValueOnce('')         // statusBeforeReview
      .mockReturnValueOnce('def456')   // headAfterPass1 — different → fixes applied → triggers pass 2
      .mockReturnValueOnce('M fixed')  // statusAfterPass1
      .mockReturnValue('');            // headAfterPass2, statusAfterPass2, remaining
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // 3 calls: main task, self-review pass 1, verification pass 2
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(3);
  });

  it('skips verification pass when pass 1 finds no issues', async () => {
    // headBefore === headAfterPass1 → no fixes → no pass 2 → 2 runClaudeCode calls total
    const validJson = '{"verdict":"PASS","checks_performed":[],"issues_found":0}';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' }); // pass 1 (no fixes → no pass 2)
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit
      .mockReturnValueOnce('M file')   // hasChanges
      .mockReturnValueOnce('')         // currentHead (same as base)
      .mockReturnValueOnce('')         // uncommittedDiff
      .mockReturnValueOnce('abc123')   // headBeforeReview
      .mockReturnValueOnce('')         // statusBeforeReview
      .mockReturnValueOnce('abc123')   // headAfterPass1 — SAME → no fixes → skip pass 2
      .mockReturnValueOnce('')         // statusAfterPass1
      .mockReturnValue('');
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // Only 2 calls: main task + self-review pass 1 (no verification pass)
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
  });

  it('tracks passes and total_fixes in selfReviewOutcome metadata', async () => {
    // pass 1 applies fixes → triggers pass 2 verification; pass 2 applies no new fixes
    const validJson = '{"verdict":"PASS","checks_performed":[],"issues_found":0}';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' })  // pass 1
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' }); // pass 2
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit
      .mockReturnValueOnce('M file')   // hasChanges
      .mockReturnValueOnce('')         // currentHead (same as base)
      .mockReturnValueOnce('')         // uncommittedDiff
      .mockReturnValueOnce('abc123')   // headBeforeReview
      .mockReturnValueOnce('')         // statusBeforeReview
      .mockReturnValueOnce('def456')   // headAfterPass1 — different → fixes applied (totalFixes=1)
      .mockReturnValueOnce('M fixed')  // statusAfterPass1
      .mockReturnValueOnce('def456')   // headAfterPass2 — SAME as pass1 → no new fixes
      .mockReturnValueOnce('M fixed')  // statusAfterPass2
      .mockReturnValue('');
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const statusCall = vi.mocked(persistAndPostStatus).mock.calls.find(
      c => (c[1] as Record<string, unknown>).status === 'COMPLETED',
    );
    expect(statusCall).toBeDefined();
    const outcome = (statusCall![1] as Record<string, unknown>).self_review_outcome as { fixes_applied: boolean; passes: number; total_fixes: number };
    expect(outcome).toBeDefined();
    expect(outcome.fixes_applied).toBe(true);
    expect(outcome.passes).toBe(2);
    expect(outcome.total_fixes).toBe(1);
  });

  it('parses structured JSON from self-review output and includes it in the status payload', async () => {
    const reviewJson = JSON.stringify({
      verdict: 'PASS',
      check_results: { test_coverage: { passed: true }, type_safety: { passed: true } },
      checks_performed: ['test_coverage', 'type_safety'],
      issues_found: 0,
      files_reviewed: 2,
      truncated_files_read: [],
    });
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: `\`\`\`json\n${reviewJson}\n\`\`\``, lastResultText: '' }); // self-review
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit
      .mockReturnValueOnce('M file')   // hasChanges
      .mockReturnValue('');            // remaining (headAfterReview === headBeforeReview → no fixes)
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const statusCall = vi.mocked(persistAndPostStatus).mock.calls.find(
      c => (c[1] as Record<string, unknown>).status === 'COMPLETED',
    );
    expect(statusCall).toBeDefined();
    const outcome = (statusCall![1] as Record<string, unknown>).self_review_outcome as Record<string, unknown>;
    expect(outcome).toBeDefined();
    expect(outcome.verdict).toBe('PASS');
    expect(outcome.check_results).toEqual({ test_coverage: { passed: true }, type_safety: { passed: true } });
    expect(outcome.checks_performed).toEqual(['test_coverage', 'type_safety']);
    expect(outcome.files_reviewed).toBe(2);
    expect(outcome.parse_failed).toBeUndefined();
  });

  it('derives checks_performed from check_results keys and respects agent verdict even when a check failed', async () => {
    // When the agent reports PASS but check_results contains a passed:false
    // sub-check, we record a verdict_check_mismatch category for the human
    // reviewer but no longer override the agent's verdict — the agent is in
    // a better position to weigh ambiguous sub-checks than a hard override.
    const { getMandatoryChecks } = await import('../prompt.js');
    vi.mocked(getMandatoryChecks).mockReturnValue(['test_coverage', 'type_safety']);
    const reviewJson = JSON.stringify({
      verdict: 'PASS',
      check_results: { test_coverage: { passed: true }, type_safety: { passed: false, issue: 'Missing type annotation' } },
      issues_found: 1,
      files_reviewed: 2,
      truncated_files_read: [],
    });
    const reviewOutput = `\`\`\`json\n${reviewJson}\n\`\`\``;
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: reviewOutput, lastResultText: '' }); // self-review pass 1 (no fixes → no pass 2)
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit
      .mockReturnValueOnce('M file')   // hasChanges
      .mockReturnValue('');            // remaining
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const completedCall = vi.mocked(persistAndPostStatus).mock.calls.find(
      c => (c[1] as Record<string, unknown>).status === 'COMPLETED',
    );
    expect(completedCall).toBeDefined();
    const payload = completedCall![1] as Record<string, unknown>;
    const outcome = payload.self_review_outcome as Record<string, unknown>;
    expect(outcome).toBeDefined();
    expect(outcome.check_results).toEqual({ test_coverage: { passed: true }, type_safety: { passed: false, issue: 'Missing type annotation' } });
    expect(outcome.checks_performed).toEqual(['test_coverage', 'type_safety']);
    // Agent's PASS verdict is respected — no forced flip to FAIL.
    expect(outcome.verdict).toBe('PASS');
    // Mismatch is recorded so humans reviewing the PR can see the inconsistency.
    expect(outcome.categories).toEqual(expect.arrayContaining(['verdict_check_mismatch']));
    expect(vi.mocked(pushAndCreatePR)).toHaveBeenCalled();
  });

  it('marks self_review_failed when self-review output contains no JSON', async () => {
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'Looks good to me!', lastResultText: '' }) // pass 1: no JSON → parseFailed
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'Still no JSON', lastResultText: '' }); // retry: still no JSON → FAILED
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit
      .mockReturnValueOnce('M file')   // hasChanges
      .mockReturnValue('');            // remaining
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(persistAndPostStatus)).toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({ status: 'FAILED', failure_category: 'self_review_rejected' }),
    );
    expect(vi.mocked(pushAndCreatePR)).not.toHaveBeenCalled();
  });

  it('marks task as FAILED when self-review mandatory checks missing and retry fails', async () => {
    // pass 1 has missing mandatory checks → parseFailed=true → retry → retry has no JSON → FAILED
    const { getMandatoryChecks } = await import('../prompt.js');
    vi.mocked(getMandatoryChecks).mockReturnValue(['test_coverage', 'type_safety', 'security']);
    const reviewJson = JSON.stringify({
      verdict: 'PASS',
      checks_performed: ['test_coverage'],  // missing type_safety and security
      issues_found: 0,
      files_reviewed: 1,
      truncated_files_read: [],
    });
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: `\`\`\`json\n${reviewJson}\n\`\`\``, lastResultText: '' })  // pass 1: missing checks → parseFailed
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'Still bad output', lastResultText: '' }); // retry: no JSON → FAILED
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit
      .mockReturnValueOnce('M file')   // hasChanges
      .mockReturnValue('');            // remaining
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(persistAndPostStatus)).toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({ status: 'FAILED', failure_category: 'self_review_rejected' }),
    );
    expect(vi.mocked(pushAndCreatePR)).not.toHaveBeenCalled();
  });

  it('records feedback_categories_injected from review_issue_distribution in self-review outcome', async () => {
    const reviewJson = JSON.stringify({
      verdict: 'PASS',
      check_results: { test_coverage: { passed: true } },
      checks_performed: ['test_coverage'],
      issues_found: 0,
      files_reviewed: 1,
      truncated_files_read: [],
    });
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: `\`\`\`json\n${reviewJson}\n\`\`\``, lastResultText: '' }); // self-review
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit
      .mockReturnValueOnce('M file')   // hasChanges
      .mockReturnValue('');            // remaining (no fixes)
    const task = {
      title: 'Task', description: '', context: {},
      project_context: { review_issue_distribution: { type_safety: 0.4, test_coverage: 0.3 } },
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const statusCall = vi.mocked(persistAndPostStatus).mock.calls.find(
      c => (c[1] as Record<string, unknown>).status === 'COMPLETED',
    );
    expect(statusCall).toBeDefined();
    const outcome = (statusCall![1] as Record<string, unknown>).self_review_outcome as Record<string, unknown>;
    expect(outcome).toBeDefined();
    expect(outcome.feedback_categories_injected).toEqual(expect.arrayContaining(['type_safety', 'test_coverage']));
    expect((outcome.feedback_categories_injected as string[]).length).toBe(2);
  });

  it('omits feedback_categories_injected when review_issue_distribution is absent', async () => {
    const reviewJson = JSON.stringify({
      verdict: 'PASS',
      check_results: { test_coverage: { passed: true } },
      checks_performed: ['test_coverage'],
      issues_found: 0,
      files_reviewed: 1,
      truncated_files_read: [],
    });
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: `\`\`\`json\n${reviewJson}\n\`\`\``, lastResultText: '' });
    vi.mocked(git)
      .mockReturnValueOnce('')
      .mockReturnValueOnce('M file')
      .mockReturnValue('');
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const statusCall = vi.mocked(persistAndPostStatus).mock.calls.find(
      c => (c[1] as Record<string, unknown>).status === 'COMPLETED',
    );
    expect(statusCall).toBeDefined();
    const outcome = (statusCall![1] as Record<string, unknown>).self_review_outcome as Record<string, unknown>;
    expect(outcome).toBeDefined();
    expect(outcome.feedback_categories_injected).toBeUndefined();
  });

  it('runs runProjectLintChecks after self-review applies fixes', async () => {
    // headBeforeReview !== headAfterPass1 → pass1FixesApplied=true → runProjectLintChecks called
    // pass 2 also runs with valid JSON to prevent the validation gate from blocking the task
    const validJson = '{"verdict":"PASS","checks_performed":[],"issues_found":0}';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' })  // pass 1
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' }); // pass 2
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit (rev-parse HEAD)
      .mockReturnValueOnce('M file')   // status --porcelain → hasChanges
      .mockReturnValueOnce('')         // currentHead (same as base)
      .mockReturnValueOnce('')         // uncommittedDiff for self-review (diff HEAD)
      .mockReturnValueOnce('abc123')   // headBeforeReview (rev-parse HEAD)
      .mockReturnValueOnce('')         // statusBeforeReview
      .mockReturnValueOnce('def456')   // headAfterPass1 — different → fixes applied → pass 2
      .mockReturnValueOnce('')         // statusAfterPass1
      .mockReturnValue('');            // headAfterPass2, statusAfterPass2, remaining
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(runProjectLintChecks)).toHaveBeenCalledTimes(1);
  });

  it('does not run runProjectLintChecks when self-review applies no fixes', async () => {
    // headBeforeReview === headAfterPass1 → no fixes → no pass 2 → runProjectLintChecks not called
    const validJson = '{"verdict":"PASS","checks_performed":[],"issues_found":0}';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' }); // pass 1 (no fixes → no pass 2)
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit
      .mockReturnValueOnce('M file')   // hasChanges
      .mockReturnValueOnce('')         // currentHead (same as base)
      .mockReturnValueOnce('')         // uncommittedDiff for self-review (diff HEAD)
      .mockReturnValueOnce('abc123')   // headBeforeReview
      .mockReturnValueOnce('')         // statusBeforeReview
      .mockReturnValueOnce('abc123')   // headAfterPass1 — SAME → no fixes → no pass 2
      .mockReturnValueOnce('')         // statusAfterPass1 — SAME
      .mockReturnValue('');
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(runProjectLintChecks)).not.toHaveBeenCalled();
  });

  it('task fails with lint_failed_after_self_review when runProjectLintChecks throws and failOnTestFailure is true', async () => {
    // Same fixesApplied=true sequence, but runProjectLintChecks throws a lint error
    // handleCreate has failOnTestFailure=true → task should FAIL, not complete
    vi.mocked(runProjectLintChecks).mockImplementationOnce(() => {
      throw new Error('ESLint: 3 errors found');
    });
    const validJson = '{"verdict":"PASS","checks_performed":[],"issues_found":0}';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' })  // pass 1
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' }); // pass 2
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit
      .mockReturnValueOnce('M file')   // hasChanges
      .mockReturnValueOnce('')         // currentHead (same as base)
      .mockReturnValueOnce('')         // uncommittedDiff for self-review (diff HEAD)
      .mockReturnValueOnce('abc123')   // headBeforeReview
      .mockReturnValueOnce('')         // statusBeforeReview
      .mockReturnValueOnce('def456')   // headAfterPass1 — different → fixes applied → pass 2
      .mockReturnValueOnce('')         // statusAfterPass1
      .mockReturnValue('');            // headAfterPass2, statusAfterPass2, remaining
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const failedCall = vi.mocked(persistAndPostStatus).mock.calls.find(
      c => (c[1] as Record<string, unknown>).status === 'FAILED',
    );
    expect(failedCall).toBeDefined();
    expect((failedCall![1] as Record<string, unknown>).failure_category).toBe('lint_failed_after_self_review');
    expect(vi.mocked(pushAndCreatePR)).not.toHaveBeenCalled();
  });

  it('task completes even when runProjectLintChecks throws after self-review fixes when failOnTestFailure is false', async () => {
    // handleQA has failOnTestFailure=false (default) → lint failure is only a warning, task completes
    vi.mocked(runProjectLintChecks).mockImplementationOnce(() => {
      throw new Error('ESLint: 3 errors found');
    });
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit
      .mockReturnValueOnce('M file')   // hasChanges
      .mockReturnValueOnce('')         // currentHead (same as base)
      .mockReturnValueOnce('')         // uncommittedDiff for self-review (diff HEAD)
      .mockReturnValueOnce('abc123')   // headBeforeReview
      .mockReturnValueOnce('')         // statusBeforeReview
      .mockReturnValueOnce('def456')   // headAfterReview — different → fixes applied
      .mockReturnValueOnce('')         // statusAfterReview
      .mockReturnValue('');
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleQA(TASK_ID, {}, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const completedCall = vi.mocked(persistAndPostStatus).mock.calls.find(
      c => (c[1] as Record<string, unknown>).status === 'COMPLETED',
    );
    expect(completedCall).toBeDefined();
    expect(vi.mocked(pushAndCreatePR)).toHaveBeenCalled();
  });

  it('marks task as FAILED with self_review_rejected when self-review verdict is FAIL', async () => {
    const reviewJson = JSON.stringify({
      verdict: 'FAIL',
      checks_performed: [],
      issues_found: 2,
    });
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: `\`\`\`json\n${reviewJson}\n\`\`\``, lastResultText: '' }); // self-review: FAIL verdict
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit
      .mockReturnValueOnce('M file')   // hasChanges
      .mockReturnValueOnce('')         // currentHead (same as base)
      .mockReturnValueOnce('')         // uncommittedDiff for self-review (diff HEAD)
      .mockReturnValueOnce('abc123')   // headBeforeReview
      .mockReturnValueOnce('')         // statusBeforeReview
      .mockReturnValueOnce('abc123')   // headAfterPass1 — SAME → no fixes → no pass 2
      .mockReturnValueOnce('')         // statusAfterPass1 — SAME
      .mockReturnValue('');
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(persistAndPostStatus)).toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({ status: 'FAILED', failure_category: 'self_review_rejected' }),
    );
    expect(vi.mocked(pushAndCreatePR)).not.toHaveBeenCalled();
  });

  it('does not retry on verdict/check_results mismatch — records the mismatch category but proceeds to PR with agent verdict', async () => {
    // Previously, an agent PASS verdict combined with a passed:false sub-check
    // triggered a retry loop and eventually forced a FAIL. That hard rule kept
    // killing ambitious tasks where the agent had weighed an ambiguous sub-check
    // and decided it was acceptable. We now record the mismatch category so the
    // human reviewer sees it, but proceed to PR with the agent's verdict.
    const { getMandatoryChecks } = await import('../prompt.js');
    vi.mocked(getMandatoryChecks).mockReturnValue(['test_coverage', 'type_safety']);
    const inconsistentJson = JSON.stringify({
      verdict: 'PASS',
      check_results: { test_coverage: { passed: true }, type_safety: { passed: false, issue: 'Missing return type' } },
      checks_performed: ['test_coverage', 'type_safety'],
      issues_found: 0,
      files_reviewed: 2,
      truncated_files_read: [],
    });
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: `\`\`\`json\n${inconsistentJson}\n\`\`\``, lastResultText: '' }); // pass 1
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit
      .mockReturnValueOnce('M file')   // hasChanges
      .mockReturnValueOnce('')         // currentHead
      .mockReturnValueOnce('')         // uncommittedDiff
      .mockReturnValueOnce('abc123')   // headBeforeReview
      .mockReturnValueOnce('')         // statusBeforeReview
      .mockReturnValueOnce('abc123')   // headAfterPass1 — SAME → no fixes → no pass 2
      .mockReturnValueOnce('')         // statusAfterPass1 — SAME
      .mockReturnValue('');
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // No FAIL status — PR should proceed.
    expect(vi.mocked(persistAndPostStatus)).not.toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({ status: 'FAILED' }),
    );
    expect(vi.mocked(pushAndCreatePR)).toHaveBeenCalled();
    // No retry triggered — only main task call + single self-review call.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    const completed = vi.mocked(persistAndPostStatus).mock.calls.find(
      c => (c[1] as Record<string, unknown>).status === 'COMPLETED',
    );
    const outcome = (completed![1] as Record<string, unknown>).self_review_outcome as Record<string, unknown>;
    expect(outcome.verdict).toBe('PASS');
    expect(outcome.categories).toEqual(expect.arrayContaining(['verdict_check_mismatch']));
  });

  it('re-runs testFixLoop after self-review applies fixes (handleCreate, main branch)', async () => {
    const validJson = '{"verdict":"PASS","checks_performed":[],"issues_found":0}';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' })  // pass 1
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' }); // pass 2
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit
      .mockReturnValueOnce('M file')   // hasChanges
      .mockReturnValueOnce('')         // currentHead (same as base)
      .mockReturnValueOnce('')         // uncommittedDiff for self-review (diff HEAD)
      .mockReturnValueOnce('abc123')   // headBeforeReview
      .mockReturnValueOnce('')         // statusBeforeReview
      .mockReturnValueOnce('def456')   // headAfterPass1 — different → fixes applied → pass 2
      .mockReturnValueOnce('')         // statusAfterPass1
      .mockReturnValue('');
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // testFixLoop should be called twice: once in the main pipeline, once after self-review fixes
    expect(vi.mocked(testFixLoop)).toHaveBeenCalledTimes(2);
  });

  it('does not re-run testFixLoop when self-review applies no fixes', async () => {
    const validJson = '{"verdict":"PASS","checks_performed":[],"issues_found":0}';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' }); // pass 1 (no fixes → no pass 2)
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit
      .mockReturnValueOnce('M file')   // hasChanges
      .mockReturnValueOnce('')         // currentHead (same as base)
      .mockReturnValueOnce('')         // uncommittedDiff for self-review (diff HEAD)
      .mockReturnValueOnce('abc123')   // headBeforeReview
      .mockReturnValueOnce('')         // statusBeforeReview
      .mockReturnValueOnce('abc123')   // headAfterPass1 — SAME → no fixes
      .mockReturnValueOnce('')         // statusAfterPass1 — SAME
      .mockReturnValue('');
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // Only the main pipeline call — no re-run
    expect(vi.mocked(testFixLoop)).toHaveBeenCalledTimes(1);
  });

  it('marks task as FAILED with test_failed_after_self_review when re-run tests fail and cannot be fixed', async () => {
    const validJson = '{"verdict":"PASS","checks_performed":[],"issues_found":0}';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' })  // pass 1
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' }); // pass 2
    // First testFixLoop call (main pipeline) succeeds; second (post-self-review) reports failure
    vi.mocked(testFixLoop)
      .mockResolvedValueOnce({ testFailuresDetected: false, passed: true })
      .mockResolvedValueOnce({ testFailuresDetected: true, passed: false, failureSummary: 'test_regression failed' });
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit
      .mockReturnValueOnce('M file')   // hasChanges
      .mockReturnValueOnce('')         // currentHead (same as base)
      .mockReturnValueOnce('')         // uncommittedDiff for self-review (diff HEAD)
      .mockReturnValueOnce('abc123')   // headBeforeReview
      .mockReturnValueOnce('')         // statusBeforeReview
      .mockReturnValueOnce('def456')   // headAfterPass1 — fixes applied
      .mockReturnValueOnce('')         // statusAfterPass1
      .mockReturnValue('');
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const failedCall = vi.mocked(persistAndPostStatus).mock.calls.find(
      c => (c[1] as Record<string, unknown>).status === 'FAILED',
    );
    expect(failedCall).toBeDefined();
    expect((failedCall![1] as Record<string, unknown>).failure_category).toBe('test_failed_after_self_review');
    expect((failedCall![1] as Record<string, unknown>).test_failure_summary).toBe('test_regression failed');
    expect(vi.mocked(pushAndCreatePR)).not.toHaveBeenCalled();
  });

  it('does not re-run testFixLoop for handleQA (failOnTestFailure=false) after self-review fixes', async () => {
    const validJson = '{"verdict":"PASS","checks_performed":[],"issues_found":0}';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' })  // pass 1
      .mockResolvedValueOnce({ exitCode: 0, allOutput: validJson, lastResultText: '' }); // pass 2
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit
      .mockReturnValueOnce('M file')   // hasChanges
      .mockReturnValueOnce('')         // currentHead (same as base)
      .mockReturnValueOnce('')         // uncommittedDiff for self-review (diff HEAD)
      .mockReturnValueOnce('abc123')   // headBeforeReview
      .mockReturnValueOnce('')         // statusBeforeReview
      .mockReturnValueOnce('def456')   // headAfterPass1 — fixes applied
      .mockReturnValueOnce('')         // statusAfterPass1
      .mockReturnValue('');
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleQA(TASK_ID, {}, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // QA has failOnTestFailure=false AND runTestFix=false, so no test runs at all
    expect(vi.mocked(testFixLoop)).not.toHaveBeenCalled();
  });

  it('includes feedback_categories_injected in self_review_outcome when reviewIssueDistribution is present', async () => {
    const passJson = '{"verdict":"PASS","checks_performed":[],"issues_found":0}';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: passJson, lastResultText: '' }); // self-review pass 1 (no fixes → no pass 2)
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit (rev-parse HEAD)
      .mockReturnValueOnce('M file')   // hasChanges (status --porcelain)
      .mockReturnValueOnce('')         // currentHead (same as base)
      .mockReturnValueOnce('')         // uncommittedDiff (diff HEAD)
      .mockReturnValueOnce('abc123')   // headBeforeReview
      .mockReturnValueOnce('')         // statusBeforeReview
      .mockReturnValueOnce('abc123')   // headAfterPass1 — SAME → no fixes → no pass 2
      .mockReturnValueOnce('')         // statusAfterPass1 — SAME
      .mockReturnValue('');
    const task = {
      title: 'Task',
      description: '',
      context: {},
      project_context: {
        review_issue_distribution: {
          test_coverage: 0.35,
          type_safety: 0.25,
          architecture: 0.15,
        },
      },
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const statusCall = vi.mocked(persistAndPostStatus).mock.calls.find(
      c => (c[1] as Record<string, unknown>).status === 'COMPLETED',
    );
    expect(statusCall).toBeDefined();
    const outcome = (statusCall![1] as Record<string, unknown>).self_review_outcome as Record<string, unknown>;
    expect(outcome).toBeDefined();
    const injected = outcome.feedback_categories_injected as string[];
    expect(injected).toBeDefined();
    expect(injected).toContain('test_coverage');
    expect(injected).toContain('type_safety');
    expect(injected).toContain('architecture');
  });

  it('omits feedback_categories_injected from self_review_outcome when reviewIssueDistribution is absent', async () => {
    const passJson = '{"verdict":"PASS","checks_performed":[],"issues_found":0}';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })  // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: passJson, lastResultText: '' }); // self-review pass 1 (no fixes → no pass 2)
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit (rev-parse HEAD)
      .mockReturnValueOnce('M file')   // hasChanges (status --porcelain)
      .mockReturnValueOnce('')         // currentHead (same as base)
      .mockReturnValueOnce('')         // uncommittedDiff (diff HEAD)
      .mockReturnValueOnce('abc123')   // headBeforeReview
      .mockReturnValueOnce('')         // statusBeforeReview
      .mockReturnValueOnce('abc123')   // headAfterPass1 — SAME → no fixes → no pass 2
      .mockReturnValueOnce('')         // statusAfterPass1 — SAME
      .mockReturnValue('');
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const statusCall = vi.mocked(persistAndPostStatus).mock.calls.find(
      c => (c[1] as Record<string, unknown>).status === 'COMPLETED',
    );
    expect(statusCall).toBeDefined();
    const outcome = (statusCall![1] as Record<string, unknown>).self_review_outcome as Record<string, unknown>;
    expect(outcome).toBeDefined();
    expect(outcome.feedback_categories_injected).toBeUndefined();
  });

  it('retries self-review when diff is truncated and zero files read, and succeeds when retry reports files', async () => {
    const pass1Json = '{"verdict":"PASS","checks_performed":[],"issues_found":0,"truncated_files_read":[]}';
    const retryJson = '{"verdict":"PASS","checks_performed":[],"issues_found":0,"truncated_files_read":["file1.ts","file2.ts"]}';
    const longDiff = 'x'.repeat(3500); // > UNCOMMITTED_LIMIT (3000) → triggers diffTruncated
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })         // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: pass1Json, lastResultText: '' })  // self-review pass 1 — empty truncated_files_read
      .mockResolvedValueOnce({ exitCode: 0, allOutput: retryJson, lastResultText: '' }); // truncation retry — reports files read
    vi.mocked(git)
      .mockReturnValueOnce('')                      // baseCommit
      .mockReturnValueOnce('M file')                // hasChanges
      .mockReturnValueOnce('')                      // currentHead (same as base → skip committedDiffFull git call)
      .mockReturnValueOnce(longDiff)                // uncommittedDiff → triggers diffTruncated
      .mockReturnValueOnce('file1.ts\nfile2.ts')    // nameOnly (diff --name-only base..HEAD)
      .mockReturnValueOnce('')                      // uncommittedNames (diff --name-only HEAD)
      .mockReturnValueOnce('abc123')                // headBeforeReview
      .mockReturnValueOnce('')                      // statusBeforeReview
      .mockReturnValueOnce('abc123')                // headAfterPass1 — SAME → no fixes → no pass 2
      .mockReturnValueOnce('')                      // statusAfterPass1 — SAME
      .mockReturnValue('');
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // 3 calls: main task + pass 1 + truncation retry
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(3);
    const statusCall = vi.mocked(persistAndPostStatus).mock.calls.find(
      c => (c[1] as Record<string, unknown>).status === 'COMPLETED',
    );
    expect(statusCall).toBeDefined();
    const outcome = (statusCall![1] as Record<string, unknown>).self_review_outcome as Record<string, unknown>;
    expect(outcome.categories).toContain('truncation_not_verified');
    expect(outcome.categories).not.toContain('incomplete_review');
    expect(outcome.truncated_files_read).toEqual(['file1.ts', 'file2.ts']);
  });

  it('flags incomplete_review and forces FAIL when truncation retry also reports zero files read', async () => {
    const emptyJson = '{"verdict":"FAIL","checks_performed":[],"issues_found":1,"truncated_files_read":[]}';
    const longDiff = 'x'.repeat(3500);
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })          // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: emptyJson, lastResultText: '' })   // self-review pass 1 — FAIL, empty truncated_files_read
      .mockResolvedValueOnce({ exitCode: 0, allOutput: emptyJson, lastResultText: '' });  // truncation retry — still empty
    vi.mocked(git)
      .mockReturnValueOnce('')                  // baseCommit
      .mockReturnValueOnce('M file')            // hasChanges
      .mockReturnValueOnce('')                  // currentHead
      .mockReturnValueOnce(longDiff)            // uncommittedDiff → diffTruncated=true
      .mockReturnValueOnce('file1.ts')          // nameOnly
      .mockReturnValueOnce('')                  // uncommittedNames
      .mockReturnValueOnce('abc123')            // headBeforeReview
      .mockReturnValueOnce('')                  // statusBeforeReview
      .mockReturnValueOnce('abc123')            // headAfterPass1 — SAME → no fixes
      .mockReturnValueOnce('')                  // statusAfterPass1
      .mockReturnValue('');
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // PR should NOT be created — incomplete review now forces FAIL, blocking PR
    expect(vi.mocked(pushAndCreatePR)).not.toHaveBeenCalled();
    const statusCall = vi.mocked(persistAndPostStatus).mock.calls.find(
      c => (c[1] as Record<string, unknown>).status === 'FAILED',
    );
    expect(statusCall).toBeDefined();
    const outcome = (statusCall![1] as Record<string, unknown>).self_review_outcome as Record<string, unknown>;
    expect(outcome.categories).toContain('truncation_not_verified');
    expect(outcome.categories).toContain('incomplete_review');
    expect(outcome.verdict).toBe('FAIL');
  });

  it('does not run truncation retry when failOnTestFailure is false (handleQA)', async () => {
    const emptyJson = '{"verdict":"PASS","checks_performed":[],"issues_found":0,"truncated_files_read":[]}';
    const longDiff = 'x'.repeat(3500);
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })          // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: emptyJson, lastResultText: '' });  // self-review pass 1 — no retry should follow
    vi.mocked(git)
      .mockReturnValueOnce('')
      .mockReturnValueOnce('M file')
      .mockReturnValueOnce('')
      .mockReturnValueOnce(longDiff)
      .mockReturnValueOnce('file1.ts')
      .mockReturnValueOnce('')
      .mockReturnValueOnce('abc123')
      .mockReturnValueOnce('')
      .mockReturnValueOnce('abc123')
      .mockReturnValueOnce('')
      .mockReturnValue('');
    const task = { title: 'QA task', description: '' };
    await handleQA(TASK_ID, {}, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // Only 2 calls: main task + pass 1. No truncation retry because handleQA has failOnTestFailure=false.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    const statusCall = vi.mocked(persistAndPostStatus).mock.calls.find(
      c => (c[1] as Record<string, unknown>).status === 'COMPLETED',
    );
    const outcome = (statusCall![1] as Record<string, unknown>).self_review_outcome as Record<string, unknown>;
    expect(outcome.categories).toContain('truncation_not_verified');
    expect(outcome.categories).not.toContain('incomplete_review');
  });

  it('retries when partial file coverage (<80%) and flags incomplete_review with unreviewed files if still below threshold', async () => {
    // 5 changed files; pass1 reads 1 (20% coverage); retry reads 2 more (union=3 of 5 = 60% < 80%).
    const partialPass1 = '{"verdict":"PASS","checks_performed":[],"issues_found":0,"truncated_files_read":["a.ts"]}';
    const partialRetry = '{"verdict":"PASS","checks_performed":[],"issues_found":0,"truncated_files_read":["b.ts","c.ts"]}';
    const longDiff = 'x'.repeat(3500);
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })             // main task
      .mockResolvedValueOnce({ exitCode: 0, allOutput: partialPass1, lastResultText: '' })   // pass 1 — only 1 of 5
      .mockResolvedValueOnce({ exitCode: 0, allOutput: partialRetry, lastResultText: '' }); // retry — 2 more, still 3/5
    vi.mocked(git)
      .mockReturnValueOnce('')                                  // baseCommit
      .mockReturnValueOnce('M file')                            // hasChanges
      .mockReturnValueOnce('')                                  // currentHead
      .mockReturnValueOnce(longDiff)                            // uncommittedDiff → truncated
      .mockReturnValueOnce('a.ts\nb.ts\nc.ts\nd.ts\ne.ts')      // nameOnly
      .mockReturnValueOnce('')                                  // uncommittedNames
      .mockReturnValueOnce('abc123')                            // headBeforeReview
      .mockReturnValueOnce('')                                  // statusBeforeReview
      .mockReturnValueOnce('abc123')                            // headAfterPass1 — SAME
      .mockReturnValueOnce('')                                  // statusAfterPass1
      .mockReturnValue('');
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(3); // retry did run
    const statusCall = vi.mocked(persistAndPostStatus).mock.calls.find(
      c => (c[1] as Record<string, unknown>).status === 'FAILED',
    );
    const outcome = (statusCall![1] as Record<string, unknown>).self_review_outcome as Record<string, unknown>;
    expect(outcome.categories).toContain('truncation_not_verified');
    expect(outcome.categories).toContain('incomplete_review');
    expect(outcome.verdict).toBe('FAIL');
    expect(outcome.unreviewed_files).toEqual(['d.ts', 'e.ts']);
    expect(outcome.truncated_files_read).toEqual(expect.arrayContaining(['a.ts', 'b.ts', 'c.ts']));
  });
});

// ── testFixLoop receives buildConfig from task.build_config ───────────────────
describe('testFixLoop buildConfig propagation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Make git('status --porcelain') return a non-empty string so hasChanges=true
    // and the test fix branch is entered. Call order: rev-parse (baseCommit),
    // status --porcelain (diff), rev-parse (currentHead), diff --name-only,
    // add -A, status --porcelain (staged), rev-parse (commitHash).
    vi.mocked(git)
      .mockReturnValueOnce('')        // rev-parse HEAD → baseCommit
      .mockReturnValueOnce('M file')  // status --porcelain → hasChanges = true
      .mockReturnValue('');           // all remaining calls
  });

  it('passes build_config from task to testFixLoop when running test fix', async () => {
    const buildConfig = {
      directories: [
        { path: 'backend', type: 'python', test_cmd: 'pytest -x -q' },
      ],
    };
    const task = {
      title: 'Task',
      description: '',
      context: {},
      project_context: {},
      build_config: buildConfig,
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // testFixLoop should be called with the buildConfig from task.build_config.
    // Baseline defaults to undefined since captureTestBaseline's default mock resolves to null.
    expect(vi.mocked(testFixLoop)).toHaveBeenCalledWith(
      WS, expect.any(Object), undefined, undefined, buildConfig, undefined, [], undefined,
    );
  });

  it('passes empty buildConfig to testFixLoop when task has no build_config', async () => {
    vi.mocked(git)
      .mockReturnValueOnce('')        // rev-parse HEAD → baseCommit
      .mockReturnValueOnce('M file')  // status --porcelain → hasChanges = true
      .mockReturnValue('');
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(testFixLoop)).toHaveBeenCalledWith(
      WS, expect.any(Object), undefined, undefined, undefined, undefined, [], undefined,
    );
  });
});

// ── Test baseline capture ─────────────────────────────────────────────────────
describe('test baseline capture', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(git)
      .mockReturnValueOnce('')        // rev-parse HEAD → baseCommit
      .mockReturnValueOnce('M file')  // status --porcelain → hasChanges = true
      .mockReturnValue('');           // all remaining calls
  });

  it('calls captureTestBaseline for medium scope create tasks', async () => {
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).toHaveBeenCalled();
  });

  it('calls captureTestBaseline for large scope create tasks', async () => {
    const task = { title: 'Task', description: '', context: { scope: 'large' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).toHaveBeenCalled();
  });

  it('calls captureTestBaseline for small scope create tasks', async () => {
    const task = { title: 'Task', description: '', context: { scope: 'small' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).toHaveBeenCalled();
  });

  it('calls captureTestBaseline when scope is not set', async () => {
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).toHaveBeenCalled();
  });

  it('calls captureTestBaseline for medium scope refactor tasks', async () => {
    const task = { title: 'Refactor task', description: '', context: { scope: 'medium' } };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).toHaveBeenCalled();
  });

  it('calls captureTestBaseline for large scope refactor tasks', async () => {
    const task = { title: 'Refactor task', description: '', context: { scope: 'large' } };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).toHaveBeenCalled();
  });

  it('calls captureTestBaseline for small scope refactor tasks', async () => {
    const task = { title: 'Refactor task', description: '', context: { scope: 'small' } };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).toHaveBeenCalled();
  });

  it('passes baseline result to testFixLoop when captureTestBaseline returns a Set', async () => {
    const baseline = new Set(['backend/testing/test_foo.py::test_bar']);
    vi.mocked(captureTestBaseline).mockResolvedValueOnce(baseline);
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(testFixLoop)).toHaveBeenCalledWith(
      WS, expect.any(Object), undefined, undefined, undefined, baseline, [], undefined,
    );
  });

  it('passes undefined baseline to testFixLoop when captureTestBaseline returns null (timed out)', async () => {
    vi.mocked(captureTestBaseline).mockResolvedValueOnce(null);
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(testFixLoop)).toHaveBeenCalledWith(
      WS, expect.any(Object), undefined, undefined, undefined, undefined, [], undefined,
    );
  });

  it('passes related_files from payload to captureTestBaseline', async () => {
    vi.mocked(git)
      .mockReturnValueOnce('')
      .mockReturnValueOnce('M file')
      .mockReturnValue('');
    const relatedFiles = ['agent/src/index.ts', 'agent/src/handlers/standard.ts'];
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something', related_files: relatedFiles }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).toHaveBeenCalledWith(
      WS, expect.any(Object), undefined, relatedFiles,
    );
  });

  it('passes related_files from payload to testFixLoop', async () => {
    vi.mocked(git)
      .mockReturnValueOnce('')
      .mockReturnValueOnce('M file')
      .mockReturnValue('');
    const relatedFiles = ['agent/src/index.ts'];
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something', related_files: relatedFiles }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(testFixLoop)).toHaveBeenCalledWith(
      WS, expect.any(Object), undefined, undefined, undefined, undefined, relatedFiles, undefined,
    );
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
    // First call is the analysis (read-only), second is the main invocation
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    const analysisCallArgs = vi.mocked(runClaudeCode).mock.calls[0];
    // Analysis call passes disallowedTools including Write, Edit, NotebookEdit
    expect(analysisCallArgs[5]).toEqual(expect.arrayContaining(['Write', 'Edit', 'NotebookEdit']));
  });

  it('skips analysis phase when scope is "small"', async () => {
    const task = {
      title: 'Task',
      description: '',
      context: { scope: 'small' },
      project_context: {},
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
  });

  it('skips analysis phase when scope is not set', async () => {
    const task = {
      title: 'Task',
      description: '',
      context: {},
      project_context: {},
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
  });

  it('prepends analysis result to main prompt when analysis succeeds', async () => {
    const analysisOutput = 'Files to change: foo.ts\nInterfaces to preserve: Foo interface';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: analysisOutput })
      .mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    const task = {
      title: 'Task',
      description: '',
      context: { scope: 'medium' },
      project_context: {},
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const mainPrompt = vi.mocked(runClaudeCode).mock.calls[1][0] as string;
    expect(mainPrompt).toContain('Pre-Implementation Analysis');
    expect(mainPrompt).toContain(analysisOutput);
  });

  it('proceeds without analysis context when analysis exits non-zero', async () => {
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 1, allOutput: 'error', lastResultText: '' })
      .mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    const task = {
      title: 'Task',
      description: '',
      context: { scope: 'medium' },
      project_context: {},
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const mainPrompt = vi.mocked(runClaudeCode).mock.calls[1][0] as string;
    expect(mainPrompt).not.toContain('Pre-Implementation Analysis');
  });

  it('proceeds without analysis context when analysis returns empty output', async () => {
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '   ' })
      .mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    const task = {
      title: 'Task',
      description: '',
      context: { scope: 'medium' },
      project_context: {},
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const mainPrompt = vi.mocked(runClaudeCode).mock.calls[1][0] as string;
    expect(mainPrompt).not.toContain('Pre-Implementation Analysis');
  });

  it('runs analysis phase for medium-scope refactor tasks', async () => {
    const task = {
      title: 'Refactor task',
      description: '',
      context: { scope: 'medium' },
    };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    const analysisCallArgs = vi.mocked(runClaudeCode).mock.calls[0];
    expect(analysisCallArgs[5]).toEqual(expect.arrayContaining(['Write', 'Edit', 'NotebookEdit']));
  });

  it('passes analysis context to testFixLoop when medium-scope analysis succeeds', async () => {
    vi.mocked(git)
      .mockReturnValueOnce('')        // rev-parse HEAD → baseCommit
      .mockReturnValueOnce('M file')  // status --porcelain → hasChanges = true
      .mockReturnValue('');
    const analysisOutput = '**Key dependencies**\nfoo.ts depends on bar.ts';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: analysisOutput })
      .mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    const task = {
      title: 'Task',
      description: '',
      context: { scope: 'medium' },
      project_context: {},
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(testFixLoop)).toHaveBeenCalledWith(
      WS, expect.any(Object), undefined, undefined, undefined, undefined, [],
      expect.stringContaining('Pre-Implementation Analysis'),
    );
  });

  it('passes undefined analysis context to testFixLoop when analysis fails', async () => {
    vi.mocked(git)
      .mockReturnValueOnce('')
      .mockReturnValueOnce('M file')
      .mockReturnValue('');
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 1, allOutput: 'error', lastResultText: '' })
      .mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    const task = {
      title: 'Task',
      description: '',
      context: { scope: 'medium' },
      project_context: {},
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(testFixLoop)).toHaveBeenCalledWith(
      WS, expect.any(Object), undefined, undefined, undefined, undefined, [], undefined,
    );
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
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: analysisOutput })
      .mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // Exactly one analysis run + one implementation run; no narrowing retry
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(persistAndPostStatus)).not.toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({ failure_category: 'scope_overflow' }),
    );
  });

  it('passes cochange_files from project_context through to buildAnalysisPrompt', async () => {
    vi.mocked(parseAnalysisPlanFiles).mockReturnValue(['a.ts']);
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '**Files to change**\n- a.ts' })
      .mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
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
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '**Files to change**\n- a.ts\n- b.ts' })
      .mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // analysis + narrowing retry + implementation
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(3);
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
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '**Files to change**\n- a.ts' })
      .mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
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
      expect(typeof failureCall[1].analysis_plan).toBe('string');
      expect(failureCall[1].scope_gate_triggered).toBe('analysis_overflow');
    }
    // implementation run must NOT have happened (only analysis + retry)
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(pushAndCreatePR)).not.toHaveBeenCalled();
  });

  it('does not count test files toward the production-file budget', async () => {
    // 6 production files (≤ production limit 10) + many test files: should pass without narrowing.
    const production = ['a.ts', 'b.ts', 'c.ts', 'd.ts', 'e.ts', 'f.ts'];
    const tests = ['a.test.ts', 'b.test.ts', 'c.test.ts', 'd.test.ts', 'e.test.ts', 'f.test.ts'];
    vi.mocked(parseAnalysisPlanFiles).mockReturnValue(production);
    vi.mocked(parseAnalysisPlanTestFiles).mockReturnValue(tests);
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '**Files to change**\n- a.ts' })
      .mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // Exactly one analysis run + one implementation run; no narrowing retry triggered by tests.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
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
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '**Files to change**\n- a.ts' })
      .mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // One analysis run + one implementation run; no narrowing retry.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(persistAndPostStatus)).not.toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({ failure_category: 'scope_overflow' }),
    );
  });

  it('includes analysis_plan in COMPLETED payload so siblings can learn from successful plans', async () => {
    vi.mocked(parseAnalysisPlanFiles).mockReturnValue(['a.ts', 'b.ts']);
    vi.mocked(lintFixLoop).mockResolvedValue(true);
    vi.mocked(testFixLoop).mockResolvedValue({ testFailuresDetected: false, passed: true });
    const analysisOutput = '**Files to change**\n- a.ts\n- b.ts';
    const validSelfReviewOutput = '```json\n' + JSON.stringify({
      verdict: 'PASS',
      checks_performed: ['correctness', 'test_coverage', 'type_safety', 'scope_violations', 'security', 'pattern_violations', 'cross_file_consistency', 'backwards_compatibility', 'data_model_safety', 'import_verification'],
      issues_found: 0,
      files_reviewed: 1,
      truncated_files_read: [],
    }) + '\n```';
    vi.mocked(runClaudeCode)
      .mockReset()
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: analysisOutput })  // analysis
      .mockResolvedValue({ exitCode: 0, allOutput: validSelfReviewOutput, lastResultText: '' });
    vi.mocked(git)
      .mockReset()
      .mockReturnValueOnce('')          // 1: baseCommit
      .mockReturnValueOnce('M file')    // 2: hasChanges
      .mockReturnValueOnce('')          // 3: currentHead
      .mockReturnValueOnce('')          // 4: uncommittedDiff for self-review
      .mockReturnValueOnce('abc123')    // 5: headBeforeReview
      .mockReturnValueOnce('')          // 6: statusBeforeReview
      .mockReturnValueOnce('abc123')    // 7: headAfterReview (same → no fixes)
      .mockReturnValueOnce('')          // 8: statusAfterReview
      .mockReturnValueOnce('')          // 9: postReviewDiff (scope overflow check)
      .mockReturnValue('');
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(pushAndCreatePR)).toHaveBeenCalled();
    const completedCall = vi.mocked(persistAndPostStatus).mock.calls.find(
      c => (c[1] as Record<string, unknown>).status === 'COMPLETED'
        && (c[1] as Record<string, unknown>).artifact_type === 'PULL_REQUEST',
    );
    expect(completedCall).toBeDefined();
    const body = completedCall![1] as Record<string, unknown>;
    expect(typeof body.analysis_plan).toBe('string');
    expect(body.analysis_plan as string).toContain('Pre-Implementation Analysis');
    // Field must be analysis_plan (schema-defined), not analysis_summary (silently dropped)
    expect(body).not.toHaveProperty('analysis_summary');
  });
});

// ── Lint failure handling ─────────────────────────────────────────────────────
describe('lint failure handling', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(git)
      .mockReturnValueOnce('')        // rev-parse HEAD → baseCommit
      .mockReturnValueOnce('M file')  // status --porcelain → hasChanges = true
      .mockReturnValue('');           // all remaining calls
  });

  it('marks task as FAILED when lintFixLoop returns false for a create task', async () => {
    vi.mocked(lintFixLoop).mockResolvedValue(false);
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(persistAndPostStatus)).toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({ status: 'FAILED', failure_category: 'lint_failed' }),
    );
    expect(vi.mocked(pushAndCreatePR)).not.toHaveBeenCalled();
  });

  it('does not mark task as FAILED when lintFixLoop returns true', async () => {
    vi.mocked(lintFixLoop).mockResolvedValue(true);
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(persistAndPostStatus)).not.toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({ status: 'FAILED', failure_category: 'lint_failed' }),
    );
  });

  it('marks refactor task as FAILED when tests fail after max fix attempts', async () => {
    vi.mocked(testFixLoop).mockResolvedValue({ testFailuresDetected: true, passed: false });
    const task = { title: 'Refactor task', description: '' };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(persistAndPostStatus)).toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({ status: 'FAILED', failure_category: 'test_failed' }),
    );
    expect(vi.mocked(pushAndCreatePR)).not.toHaveBeenCalled();
  });
});

// ── Scope overflow warning (soft-mode) ────────────────────────────────────────
// Scope overflow no longer hard-fails small- or medium-scope tasks. It only
// surfaces as a warning in the PR body so a human reviewer can judge whether
// the file set is too broad — hard gates were killing ambitious tasks that
// legitimately needed broader changes.
describe('scope overflow warning (soft-mode)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(testFixLoop).mockResolvedValue({ testFailuresDetected: false, passed: true });
    vi.mocked(git)
      .mockReturnValueOnce('')   // baseCommit (rev-parse HEAD)
      .mockReturnValueOnce(      // status --porcelain → 7 changed files
        'M file1.ts\nM file2.ts\nM file3.ts\nM file4.ts\nM file5.ts\nM file6.ts\nM file7.ts',
      )
      .mockReturnValue('');      // currentHead + all remaining
  });

  it('does not fail a small-scope task even when changed files exceed the threshold — proceeds to PR', async () => {
    const task = { title: 'Task', description: '', context: { scope: 'small' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(persistAndPostStatus)).not.toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({ failure_category: 'scope_overflow' }),
    );
    expect(vi.mocked(pushAndCreatePR)).toHaveBeenCalled();
  });

  it('does not fail a medium-scope task when changed files exceed the small threshold', async () => {
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(persistAndPostStatus)).not.toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({ failure_category: 'scope_overflow' }),
    );
  });

  it('surfaces a scope overflow warning in the PR body when non-test files exceed the threshold', async () => {
    const task = { title: 'Task', description: '', context: { scope: 'small' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(pushAndCreatePR)).toHaveBeenCalled();
    const prBodyArg = vi.mocked(pushAndCreatePR).mock.calls[0][6] as string;
    expect(prBodyArg).toContain('Scope Overflow Warning');
    expect(prBodyArg).toContain('7 non-test files');
  });

  it('does not surface a scope overflow warning when only test files push the count over', async () => {
    vi.mocked(git)
      .mockReset()
      .mockReturnValueOnce('')  // baseCommit
      // 2 production + 5 test files = 7 total, but only 2 non-test → within threshold
      .mockReturnValueOnce(
        'M src/file1.ts\nM src/file2.ts\nM src/file1.test.ts\nM src/file2.test.ts\nM testing/test_foo.py\nM src/__tests__/bar.ts\nM src/baz.spec.ts',
      )
      .mockReturnValue('');
    const task = { title: 'Task', description: '', context: { scope: 'small' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(pushAndCreatePR)).toHaveBeenCalled();
    const prBodyArg = vi.mocked(pushAndCreatePR).mock.calls[0][6] as string;
    expect(prBodyArg).not.toContain('Scope Overflow Warning');
  });
});

// ── Refactor blocking gates ───────────────────────────────────────────────────
describe('handleRefactor blocking gates', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(git)
      .mockReturnValueOnce('')        // rev-parse HEAD → baseCommit
      .mockReturnValueOnce('M file')  // status --porcelain → hasChanges = true
      .mockReturnValue('');           // all remaining calls
  });

  it('marks task as FAILED when lint fails for refactor', async () => {
    vi.mocked(lintFixLoop).mockResolvedValue(false);
    const task = { title: 'Refactor task', description: '', context: {}, project_context: {} };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(persistAndPostStatus)).toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({ status: 'FAILED', failure_category: 'lint_failed' }),
    );
    expect(vi.mocked(pushAndCreatePR)).not.toHaveBeenCalled();
  });

  it('marks task as FAILED when tests fail for refactor', async () => {
    vi.mocked(lintFixLoop).mockResolvedValue(true);
    vi.mocked(testFixLoop).mockResolvedValue({ testFailuresDetected: true, passed: false, failureSummary: 'test_foo failed' });
    const task = { title: 'Refactor task', description: '', context: {}, project_context: {} };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(persistAndPostStatus)).toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({ status: 'FAILED', failure_category: 'test_failed' }),
    );
    expect(vi.mocked(pushAndCreatePR)).not.toHaveBeenCalled();
  });

  it('calls captureTestBaseline for medium scope refactor tasks', async () => {
    const task = { title: 'Refactor task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).toHaveBeenCalled();
  });

  it('calls captureTestBaseline for large scope refactor tasks', async () => {
    const task = { title: 'Refactor task', description: '', context: { scope: 'large' }, project_context: {} };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).toHaveBeenCalled();
  });

  it('calls captureTestBaseline for small scope refactor tasks', async () => {
    const task = { title: 'Refactor task', description: '', context: { scope: 'small' }, project_context: {} };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).toHaveBeenCalled();
  });
});

// ── Scope overflow warning in PR body (post self-review) ─────────────────────
// The post-self-review hard gate was removed: it used to block PR creation
// after ~8-10 min of successful lint/test/self-review when file count still
// exceeded the scope threshold. That killed ambitious tasks that legitimately
// needed broader changes. Now the overflow just surfaces as a warning in the
// PR body so the human reviewer can judge it.
describe('scope overflow warning in PR body (post self-review)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  function setupGitMocks(diff: string): void {
    vi.mocked(lintFixLoop).mockResolvedValue(true);
    vi.mocked(testFixLoop).mockResolvedValue({ testFailuresDetected: false, passed: true });
    const validSelfReviewOutput = '```json\n' + JSON.stringify({
      verdict: 'PASS',
      checks_performed: ['correctness', 'test_coverage', 'type_safety', 'scope_violations', 'security', 'pattern_violations', 'cross_file_consistency', 'backwards_compatibility', 'data_model_safety', 'import_verification'],
      issues_found: 0,
      files_reviewed: 1,
      truncated_files_read: [],
    }) + '\n```';
    vi.mocked(runClaudeCode).mockReset().mockResolvedValue({ exitCode: 0, allOutput: validSelfReviewOutput, lastResultText: '' });
    vi.mocked(git)
      .mockReset()
      .mockReturnValueOnce('')        // baseCommit
      .mockReturnValueOnce(diff)      // hasChanges (status --porcelain — drives changedPaths)
      .mockReturnValueOnce('')        // currentHead (same as baseCommit)
      .mockReturnValueOnce('')        // uncommittedDiff for self-review
      .mockReturnValueOnce('abc123')  // headBeforeReview
      .mockReturnValueOnce('')        // statusBeforeReview
      .mockReturnValueOnce('abc123')  // headAfterReview (same → no fixes)
      .mockReturnValueOnce('')        // statusAfterReview
      .mockReturnValue('');
  }

  it('does not block PR when medium task exceeds threshold — surfaces warning in PR body instead', async () => {
    const files11 = Array.from({ length: 11 }, (_, i) => `M src/file${i}.ts`).join('\n');
    setupGitMocks(files11);
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(persistAndPostStatus)).not.toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({ failure_category: 'scope_overflow' }),
    );
    expect(vi.mocked(pushAndCreatePR)).toHaveBeenCalled();
    const prBodyArg = vi.mocked(pushAndCreatePR).mock.calls[0][6] as string;
    expect(prBodyArg).toContain('Scope Overflow Warning');
    expect(prBodyArg).toContain('11 non-test files');
  });

  it('does not surface a warning when the medium task is within threshold', async () => {
    const files5 = Array.from({ length: 5 }, (_, i) => `M src/file${i}.ts`).join('\n');
    setupGitMocks(files5);
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(pushAndCreatePR)).toHaveBeenCalled();
    const prBodyArg = vi.mocked(pushAndCreatePR).mock.calls[0][6] as string;
    expect(prBodyArg).not.toContain('Scope Overflow Warning');
  });

  it('does not block PR when small task exceeds threshold — surfaces warning in PR body instead', async () => {
    const files7 = Array.from({ length: 7 }, (_, i) => `M src/file${i}.ts`).join('\n');
    setupGitMocks(files7);
    const task = { title: 'Task', description: '', context: { scope: 'small' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(persistAndPostStatus)).not.toHaveBeenCalledWith(
      TASK_ID,
      expect.objectContaining({ failure_category: 'scope_overflow' }),
    );
    expect(vi.mocked(pushAndCreatePR)).toHaveBeenCalled();
    const prBodyArg = vi.mocked(pushAndCreatePR).mock.calls[0][6] as string;
    expect(prBodyArg).toContain('Scope Overflow Warning');
  });

  it('does not surface a warning for test-heavy diffs — test files are excluded from the count', async () => {
    const mixedFiles = [
      'M src/prod1.ts', 'M src/prod2.ts', 'M src/prod3.ts',
      'M src/prod1.test.ts', 'M src/prod2.test.ts', 'M src/prod3.test.ts',
      'M testing/test_a.py', 'M testing/test_b.py',
      'M src/__tests__/c.ts', 'M src/__tests__/d.ts',
      'M src/helper.spec.ts', 'M src/util.spec.tsx',
    ].join('\n');
    setupGitMocks(mixedFiles);
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(pushAndCreatePR)).toHaveBeenCalled();
    const prBodyArg = vi.mocked(pushAndCreatePR).mock.calls[0][6] as string;
    expect(prBodyArg).not.toContain('Scope Overflow Warning');
  });
});

// ── buildSmallScopeCallerNote ────────────────────────────────────────────────
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
        const pattern = (args || []).find(a => typeof a === 'string' && a.includes('import')) as string | undefined;
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

// ── extractSelfReviewJson parsing ─────────────────────────────────────────────
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

describe('migration auto-generation heartbeat', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('sends heartbeat with migration_auto_generated:true after successful migration generation', async () => {
    vi.mocked(git).mockImplementation((args: readonly string[]) => {
      if (args[0] === 'status') return ' M backend/app/models.py';
      if (args[0] === 'rev-parse') return 'basecommit';
      return '';
    });
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });

    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const migrationCall = vi.mocked(sendHeartbeatSync).mock.calls.find(
      c => c[2] !== undefined && 'migration_auto_generated' in (c[2] as Record<string, unknown>),
    );
    expect(migrationCall).toBeDefined();
    expect((migrationCall![2] as Record<string, unknown>).migration_auto_generated).toBe(true);
  });

  it('sends heartbeat with migration_auto_generated:false when the migration pass exits non-zero', async () => {
    vi.mocked(git).mockImplementation((args: readonly string[]) => {
      if (args[0] === 'status') return ' M backend/app/models.py';
      if (args[0] === 'rev-parse') return 'basecommit';
      return '';
    });
    vi.mocked(runClaudeCode).mockImplementation(async (prompt: string) => ({
      exitCode: prompt.includes('did NOT add an alembic migration') ? 1 : 0,
      allOutput: '',
      lastResultText: '',
    }));

    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const migrationCall = vi.mocked(sendHeartbeatSync).mock.calls.find(
      c => c[2] !== undefined && 'migration_auto_generated' in (c[2] as Record<string, unknown>),
    );
    expect(migrationCall).toBeDefined();
    expect((migrationCall![2] as Record<string, unknown>).migration_auto_generated).toBe(false);
  });
});
