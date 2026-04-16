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
  setupWorkspace: vi.fn().mockResolvedValue({ workBranch: 'task-abc123', shortId: 'abc123' }),
  pushAndCreatePR: vi.fn().mockResolvedValue('https://github.com/owner/repo/pull/1'),
  runClaudeCode: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' }),
  matchesRateLimit: vi.fn().mockReturnValue(false),
  lintFixLoop: vi.fn().mockResolvedValue(true),
  testFixLoop: vi.fn().mockResolvedValue({ testFailuresDetected: false, passed: true }),
  captureTestBaseline: vi.fn().mockResolvedValue(null),
  runProjectLintChecks: vi.fn(),
  cloneWithRetry: vi.fn(),
  sendHeartbeatSync: vi.fn(),
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
  extractModuleInterfaces: vi.fn().mockReturnValue(''),
  discoverAffectedConsumers: vi.fn().mockReturnValue(''),
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
import { handleCreate, handleRefactor, handleDocs, handleQA, handleDeps } from './standard.js';
import { setupWorkspace, runClaudeCode, pushAndCreatePR, testFixLoop, captureTestBaseline, git, lintFixLoop, persistAndPostStatus, runProjectLintChecks } from '../index.js';
import { extractModuleInterfaces, discoverAffectedConsumers } from '../prompt.js';

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
    vi.mocked(git).mockReturnValueOnce('M src/foo.ts'); // diff has changes
    const task = { title: 'My Task', description: '', context: { issue_number: 42 } };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prBodyArg = vi.mocked(pushAndCreatePR).mock.calls[0][6] as string;
    expect(prBodyArg).toContain('Closes #42');
  });

  it('uses issue_url over issue_number in PR body when both are set', async () => {
    const { git } = await import('../index.js');
    vi.mocked(git).mockReturnValueOnce('M src/foo.ts');
    const task = { title: 'My Task', description: '', context: { issue_number: 42, issue_url: 'https://github.com/owner/repo/issues/42' } };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prBodyArg = vi.mocked(pushAndCreatePR).mock.calls[0][6] as string;
    expect(prBodyArg).toContain('Closes https://github.com/owner/repo/issues/42');
  });

  it('does not include Closes in PR body when no issue context', async () => {
    const { git } = await import('../index.js');
    vi.mocked(git).mockReturnValueOnce('M src/foo.ts');
    const task = { title: 'My Task', description: '' };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prBodyArg = vi.mocked(pushAndCreatePR).mock.calls[0][6] as string;
    expect(prBodyArg).not.toContain('Closes');
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

  it('passes captureBaseline=true for medium scope refactor', async () => {
    const task = { title: 'Refactor', description: '', context: { scope: 'medium' } };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).toHaveBeenCalled();
  });

  it('passes captureBaseline=true for large scope refactor', async () => {
    const task = { title: 'Refactor', description: '', context: { scope: 'large' } };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).toHaveBeenCalled();
  });

  it('does not capture baseline for small scope refactor', async () => {
    const task = { title: 'Refactor', description: '', context: { scope: 'small' } };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).not.toHaveBeenCalled();
  });

  it('includes planning note in prompt for medium scope refactor', async () => {
    const task = { title: 'Refactor', description: '', context: { scope: 'medium' } };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).toContain('Pre-Refactor Planning');
  });

  it('does not include planning note for small scope refactor', async () => {
    const task = { title: 'Refactor', description: '', context: { scope: 'small' } };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).not.toContain('Pre-Refactor Planning');
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

  it('calls discoverAffectedConsumers for handleRefactor with scope=medium', async () => {
    vi.mocked(discoverAffectedConsumers).mockReturnValue('## Affected Consumers\nbar\n\n');
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
  });

  it('runs self-review for handleCreate when there are changes', async () => {
    vi.mocked(git)
      .mockReturnValueOnce('')       // rev-parse HEAD → baseCommit
      .mockReturnValueOnce('M file') // status --porcelain → hasChanges = true
      .mockReturnValue('');          // all remaining git calls
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // runClaudeCode should be called twice: once for main task, once for self-review
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
  });

  it('runs self-review for handleRefactor when there are changes', async () => {
    vi.mocked(git)
      .mockReturnValueOnce('')
      .mockReturnValueOnce('M file')
      .mockReturnValue('');
    const task = { title: 'Refactor task', description: '' };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
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
    vi.mocked(git)
      .mockReturnValueOnce('')       // rev-parse HEAD → baseCommit
      .mockReturnValueOnce('M file') // status --porcelain → hasChanges = true
      .mockReturnValue('');          // all remaining git calls
    const task = { title: 'QA task', description: '' };
    await handleQA(TASK_ID, {}, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // runClaudeCode should be called twice: once for main task, once for self-review
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
  });

  it('includes self-review result in PR body when fixes were applied', async () => {
    // Simulate: main task diff, then self-review changes something (HEAD changes)
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit (rev-parse HEAD before Claude)
      .mockReturnValueOnce('M file')   // diff (status --porcelain → hasChanges)
      .mockReturnValueOnce('')         // currentHead (rev-parse HEAD after Claude, same as base)
      .mockReturnValueOnce('')         // diff --name-only committed paths
      .mockReturnValueOnce('')         // committedDiff for self-review (diff baseCommit..HEAD)
      .mockReturnValueOnce('')         // uncommittedDiff for self-review (diff HEAD)
      .mockReturnValueOnce('abc123')   // headBeforeReview (rev-parse HEAD)
      .mockReturnValueOnce('')         // statusBeforeReview (status --porcelain)
      .mockReturnValueOnce('def456')   // headAfterReview — different → fixes applied
      .mockReturnValueOnce('M fixed')  // statusAfterReview
      .mockReturnValue('');
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prBodyArg = vi.mocked(pushAndCreatePR).mock.calls[0][6] as string;
    expect(prBodyArg).toContain('Self-Review');
    expect(prBodyArg).toContain('Issues found and auto-fixed');
  });

  it('includes self-review result in PR body when no issues found', async () => {
    // headBefore === headAfter and status unchanged → no fixes applied.
    // Call sequence when currentHead === baseCommit (no committedDiff git call):
    // 1: rev-parse HEAD → baseCommit
    // 2: status --porcelain → 'M file' (hasChanges)
    // 3: rev-parse HEAD → currentHead (same as base)
    // 4: diff HEAD → uncommittedDiff (self-review, since no commits)
    // 5: rev-parse HEAD → headBeforeReview
    // 6: status --porcelain → statusBeforeReview
    // 7: rev-parse HEAD → headAfterReview — SAME → no fixes
    // 8: status --porcelain → statusAfterReview — SAME → no fixes
    vi.mocked(git)
      .mockReturnValueOnce('')         // 1: baseCommit
      .mockReturnValueOnce('M file')   // 2: hasChanges
      .mockReturnValueOnce('')         // 3: currentHead (same as base)
      .mockReturnValueOnce('')         // 4: uncommittedDiff
      .mockReturnValueOnce('abc123')   // 5: headBeforeReview
      .mockReturnValueOnce('')         // 6: statusBeforeReview
      .mockReturnValueOnce('abc123')   // 7: headAfterReview — SAME → no fixes
      .mockReturnValueOnce('')         // 8: statusAfterReview — SAME
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
    const outcome = (statusCall![1] as Record<string, unknown>).self_review_outcome as { categories: string[] };
    expect(outcome).toBeDefined();
    expect(outcome.categories).toContain('self_review_failed');
  });

  it('runs runProjectLintChecks after self-review applies fixes', async () => {
    // currentHead === baseCommit (both ''), diff has changes → changedPaths=['file']
    // headBeforeReview !== headAfterReview → fixesApplied=true → runProjectLintChecks called
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit (rev-parse HEAD)
      .mockReturnValueOnce('M file')   // status --porcelain → hasChanges
      .mockReturnValueOnce('')         // currentHead (same as base)
      .mockReturnValueOnce('')         // uncommittedDiff for self-review (diff HEAD)
      .mockReturnValueOnce('abc123')   // headBeforeReview (rev-parse HEAD)
      .mockReturnValueOnce('')         // statusBeforeReview (status --porcelain)
      .mockReturnValueOnce('def456')   // headAfterReview — different → fixes applied
      .mockReturnValueOnce('')         // statusAfterReview
      .mockReturnValue('');
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(runProjectLintChecks)).toHaveBeenCalledTimes(1);
  });

  it('does not run runProjectLintChecks when self-review applies no fixes', async () => {
    // headBeforeReview === headAfterReview → fixesApplied=false → runProjectLintChecks not called
    vi.mocked(git)
      .mockReturnValueOnce('')         // baseCommit
      .mockReturnValueOnce('M file')   // hasChanges
      .mockReturnValueOnce('')         // currentHead (same as base)
      .mockReturnValueOnce('')         // uncommittedDiff for self-review (diff HEAD)
      .mockReturnValueOnce('abc123')   // headBeforeReview
      .mockReturnValueOnce('')         // statusBeforeReview
      .mockReturnValueOnce('abc123')   // headAfterReview — SAME → no fixes
      .mockReturnValueOnce('')         // statusAfterReview — SAME
      .mockReturnValue('');
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(runProjectLintChecks)).not.toHaveBeenCalled();
  });

  it('task completes even when runProjectLintChecks throws after self-review fixes', async () => {
    // Same fixesApplied=true sequence, but runProjectLintChecks throws a lint error
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
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // Task should complete (not fail) despite lint error
    const completedCall = vi.mocked(persistAndPostStatus).mock.calls.find(
      c => (c[1] as Record<string, unknown>).status === 'COMPLETED',
    );
    expect(completedCall).toBeDefined();
    expect(vi.mocked(pushAndCreatePR)).toHaveBeenCalled();
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
    // testFixLoop should be called with the buildConfig from task.build_config and no baseline (small scope)
    expect(vi.mocked(testFixLoop)).toHaveBeenCalledWith(
      WS, expect.any(Object), undefined, undefined, buildConfig, undefined, [],
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
      WS, expect.any(Object), undefined, undefined, undefined, undefined, [],
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

  it('does not call captureTestBaseline for small scope create tasks', async () => {
    const task = { title: 'Task', description: '', context: { scope: 'small' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).not.toHaveBeenCalled();
  });

  it('does not call captureTestBaseline when scope is not set', async () => {
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).not.toHaveBeenCalled();
  });

  it('passes baseline result to testFixLoop when captureTestBaseline returns a Set', async () => {
    const baseline = new Set(['backend/testing/test_foo.py::test_bar']);
    vi.mocked(captureTestBaseline).mockResolvedValueOnce(baseline);
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(testFixLoop)).toHaveBeenCalledWith(
      WS, expect.any(Object), undefined, undefined, undefined, baseline, [],
    );
  });

  it('passes undefined baseline to testFixLoop when captureTestBaseline returns null (timed out)', async () => {
    vi.mocked(captureTestBaseline).mockResolvedValueOnce(null);
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(testFixLoop)).toHaveBeenCalledWith(
      WS, expect.any(Object), undefined, undefined, undefined, undefined, [],
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
      WS, expect.any(Object), undefined, undefined, undefined, undefined, relatedFiles,
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
});
