import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── Type-safe mock export names ────────────────────────────────────────────────
// Ensures that renaming/removing an export in the mocked module causes tsc to
// fail at compile time. Each type assertion verifies the array only contains
// valid export keys from the actual module.
const {
  INDEX_MOCK_EXPORTS,
  TEST_FIX_LOOP_MOCK_EXPORTS,
  PROMPT_MOCK_EXPORTS,
  CODE_CHANGE_MOCK_EXPORTS,
  PRIOR_KNOWLEDGE_MOCK_EXPORTS,
} = vi.hoisted(() => {
  type IndexExportKey = keyof typeof import('../index.js');
  type TestFixLoopExportKey = keyof typeof import('../utils/test-fix-loop.js');
  type PromptExportKey = keyof typeof import('../prompt.js');
  type CodeChangeExportKey = keyof typeof import('./code-change.js');
  type PriorKnowledgeExportKey = keyof typeof import('./prior-knowledge.js');

  return {
    INDEX_MOCK_EXPORTS: [
      'WORKSPACE', 'sanitize', 'sendLog', 'log', 'getAgentToken', 'apiPost',
      'apiGet', 'persistAndPostStatus', 'readHandlerOutput', 'validateBranchName',
      'git', 'setupWorkspace', 'runClaudeCode', 'sendHeartbeatSync', 'getProviderRunner',
    ] satisfies readonly IndexExportKey[],
    TEST_FIX_LOOP_MOCK_EXPORTS: [
      'captureTestBaseline',
    ] satisfies readonly TestFixLoopExportKey[],
    PROMPT_MOCK_EXPORTS: [
      'getTaskTypeHints', 'readClaudeMd', 'buildContextPrefix', 'buildFileTree',
      'discoverRelatedTests', 'configureAgentPermissions', 'writePlaybookHelper',
      'writeMessagesHelper', 'writeViewportSnapshotHelper', 'writeViewportLiveHelper',
      'writeCodeChangeHelpers', 'formatPlaybook', 'buildRetryContext', 'buildRepoMap',
      'buildAnalysisPrompt', 'parseAnalysisPlanFiles', 'parseAnalysisPlanTestFiles',
      'parseAnalysisPlanVerifyFiles', 'extractModuleInterfaces', 'discoverAffectedConsumers',
    ] satisfies readonly PromptExportKey[],
    CODE_CHANGE_MOCK_EXPORTS: [
      'runCodeChangeTask',
    ] satisfies readonly CodeChangeExportKey[],
    PRIOR_KNOWLEDGE_MOCK_EXPORTS: [
      'fetchKnowledgeDocumentById', 'fetchArtifactDownloadUrl',
    ] satisfies readonly PriorKnowledgeExportKey[],
  };
});

// Used for type checking only; suppress "unused" warnings
void INDEX_MOCK_EXPORTS; void TEST_FIX_LOOP_MOCK_EXPORTS; void PROMPT_MOCK_EXPORTS; void CODE_CHANGE_MOCK_EXPORTS; void PRIOR_KNOWLEDGE_MOCK_EXPORTS;

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
    setupWorkspace: vi.fn().mockResolvedValue({ workBranch: 'task-abc123', shortId: 'abc123' }),
    runClaudeCode: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' }),
    sendHeartbeatSync: vi.fn(),
    getProviderRunner: vi.fn().mockReturnValue({ displayName: 'Claude Code', provider: 'anthropic' }),
  };
});

// The test verification loop now lives in utils/test-fix-loop.ts; handlers import
// captureTestBaseline from there, so the mock must target that module.
vi.mock('../utils/test-fix-loop.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/test-fix-loop.js')>();
  return {
    ...actual,
    captureTestBaseline: vi.fn().mockResolvedValue(null),
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
    writeViewportSnapshotHelper: vi.fn(),
    writeViewportLiveHelper: vi.fn(),
    writeCodeChangeHelpers: vi.fn(),
    formatPlaybook: vi.fn().mockReturnValue(''),
    buildRetryContext: vi.fn().mockImplementation((task: Record<string, unknown>) => {
      const ctx = (task.context || {}) as Record<string, unknown>;
      const attemptNumber = ctx._attempt_number;
      return typeof attemptNumber === 'number' && attemptNumber > 1
        ? 'mock retry context for testing'
        : '';
    }),
    buildRepoMap: vi.fn().mockReturnValue(''),
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
// Full-replacement (not importOriginal) is intentional: the marker-detection
// tests call vi.importActual('./code-change.js') which, combined with an
// importOriginal factory, causes the real runClaudeCode to run (the async
// factory loads code-change.js before index.js's mock is fully resolved).
// All three runtime exports are listed explicitly so the factory cannot drift.
vi.mock('./code-change.js', () => ({
  runCodeChangeTask: vi.fn().mockResolvedValue(undefined),
  augmentSelfReview: vi.fn(),
  maybeRunIncompleteReviewRecovery: vi.fn().mockResolvedValue(undefined),
}));

// The '../index.js' mock above uses importOriginal, which loads the REAL index
// module graph — prior-knowledge.js gets instantiated there bound to the real
// apiGet, so the index mock never reaches its fetchers. Left unmocked, a test
// with knowledge/artifact prerequisites runs the real apiGet retry/backoff
// (~16s against nothing) and times out. Mock the fetchers directly (fail-soft
// null, same as an unreachable backend) and keep the pure section builders real
// so prompt-text assertions exercise the genuine output.
vi.mock('./prior-knowledge.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./prior-knowledge.js')>();
  return {
    ...actual,
    fetchKnowledgeDocumentById: vi.fn().mockResolvedValue(null),
    fetchArtifactDownloadUrl: vi.fn().mockResolvedValue(null),
  };
});

vi.mock('./artifact-consume.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./artifact-consume.js')>();
  return {
    ...actual,
    hydrateComputeResultArtifacts: vi.fn().mockResolvedValue({
      localPaths: new Map(),
      rejectedEntries: [],
    }),
    consumeAndCommitArtifacts: vi.fn().mockResolvedValue({
      committedArtifactIds: [],
      commits: [],
      rejectedEntries: [],
    }),
  };
});

vi.mock('../workspacePrep.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../workspacePrep.js')>();
  return { ...actual, prepareWorkspaceForRole: vi.fn().mockResolvedValue(undefined) };
});

vi.mock('../prompts/diagnostics-after-edit-hook.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../prompts/diagnostics-after-edit-hook.js')>();
  return { ...actual, writeDiagnosticsAfterEditHook: vi.fn().mockReturnValue(true) };
});

// Spy on the read-only ./fetch writer while keeping buildFetchGuidance and the
// PLAN_FETCH_VERBS constant real, so prompt-guidance assertions exercise the
// genuine text and the writer-invocation assertions can inspect the call args.
vi.mock('./strategic-helpers.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./strategic-helpers.js')>();
  return { ...actual, writeFetchHelper: vi.fn() };
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
    promises: {
      writeFile: vi.fn().mockResolvedValue(undefined),
      readFile: vi.fn().mockResolvedValue(''),
    },
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
import { handleCreate, handleRefactor, handleDocs, handleQA, handleDeps, handleSalvageBundle, extractSelfReviewJson, needsMigrationGeneration, buildSmallScopeCallerNote, computeRegressionRiskHeuristic, assignCapabilityHoldbackArm, resolveVisualAcceptance, assembleStandardPrompt, type StandardPromptSections } from './standard.js';
import { setupWorkspace, runClaudeCode, sendHeartbeatSync, getProviderRunner, git } from '../index.js';
import { PLAYBOOK_GUIDANCE, DEFAULT_TOOL_GUIDANCE } from '../prompt.js';
import { VIEWPORT_INFRA_SKIP_GUIDANCE } from './self-review-support.js';
import { resetLlmTelemetry, getLlmTelemetry, buildPromptSizeTelemetry, setPromptSizeTelemetry } from '../telemetry.js';
import { captureTestBaseline } from '../utils/test-fix-loop.js';
import { readClaudeMd, extractModuleInterfaces, discoverRelatedTests, discoverAffectedConsumers, parseAnalysisPlanFiles, parseAnalysisPlanTestFiles, parseAnalysisPlanVerifyFiles, buildAnalysisPrompt, buildRepoMap } from '../prompt.js';
import { runCodeChangeTask } from './code-change.js';
import { writeFetchHelper, PLAN_FETCH_VERBS } from './strategic-helpers.js';
import { expectCalledWith, expectNotCalledWith } from '../utils/typed-mock-call.js';
import { writeDiagnosticsAfterEditHook } from '../prompts/diagnostics-after-edit-hook.js';
import { fetchArtifactDownloadUrl } from './prior-knowledge.js';
import { hydrateComputeResultArtifacts } from './artifact-consume.js';

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

  it('injects reserved-revision guidance for a create task with a reservation', async () => {
    const task = {
      title: 'Add column',
      description: '',
      context: { related_files: ['backend/app/models.py'] },
      project_context: {},
      migration_reservation: {
        provisional_revision: '0153',
        provisional_down_revision: '0151',
      },
    };
    await handleCreate(TASK_ID, { prompt: 'Add a column' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).toContain('Reserved migration revision');
    expect(prompt).toContain('revision = "0153"');
    expect(prompt).toContain('actual pre-new-migration chain head');
    expect(prompt).toContain('advisory parent is `0151`; use it only if the actual head cannot be determined');
    expect(prompt).not.toContain('down_revision = "0151"');
    expect(prompt).toContain('EXPECTED_SCHEMA_REVISION = "0153"');
  });

  it('omits reserved-revision guidance for a create task with no reservation', async () => {
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).not.toContain('Reserved migration revision');
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

// ── Stale-premise / provenance notice injection ───────────────────────────────
describe('provenance notice injection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('omits the stale-premise notice when _provenance_warnings is absent', async () => {
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).not.toContain('## Stale-premise notice');
  });

  it('renders the stale-premise notice when _provenance_warnings is populated', async () => {
    const task = {
      title: 'Task',
      description: '',
      context: {
        _provenance_warnings: {
          cited_files_changed_since_generation: true,
          broken_source_ref_count: 1,
          broken_refs: ['app/foo.py:10'],
          ungrounded_claim_count: 1,
          ungrounded_refs: ['some claim'],
        },
      },
      project_context: {},
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).toContain('## Stale-premise notice');
    expect(prompt).toContain('Cited files changed since the finding was generated');
    expect(prompt).toContain('1 broken/missing citation(s)');
    expect(prompt).toContain('app/foo.py:10');
    expect(prompt).toContain('1 ungrounded claim(s)');
    expect(prompt).toContain('REPRODUCE-FIRST');
  });

  it('renders the notice for refactor tasks too', async () => {
    const task = {
      title: 'Refactor',
      description: '',
      context: {
        _provenance_warnings: { cited_files_changed_since_generation: true },
      },
    };
    await handleRefactor(TASK_ID, { prompt: 'Refactor it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).toContain('## Stale-premise notice');
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
    expectCalledWith(setupWorkspace,
      OWNER, REPO, GH_TOKEN, 'main', 'task', TASK_ID, WS, expect.any(Object), 'blueprint', undefined,
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

  it('uses task.project_metadata.promotion_target when payload omits promotion_target', async () => {
    const task = { title: 'Task', description: '', project_metadata: { promotion_target: 'release' } };
    await handleCreate(TASK_ID, { prompt: 'Do something', base_branch: 'release' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const opts = capturedOpts();
    expect(opts.testsOnMainBranch).toBe(true);
  });

  it('sets testsOnMainBranch=true via integration_branch cascade when promotion_target absent and baseBranch=master', async () => {
    const task = { title: 'Task', description: '', project_metadata: { integration_branch: 'master' } };
    await handleCreate(TASK_ID, { prompt: 'Do something', base_branch: 'master' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const opts = capturedOpts();
    expect(opts.baseBranch).toBe('master');
    expect(opts.testsOnMainBranch).toBe(true);
  });

  it('sets testsOnMainBranch=false when baseBranch does not match integration_branch', async () => {
    const task = { title: 'Task', description: '', project_metadata: { integration_branch: 'master' } };
    await handleCreate(TASK_ID, { prompt: 'Do something', base_branch: 'feature/x' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const opts = capturedOpts();
    expect(opts.baseBranch).toBe('feature/x');
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
    expectCalledWith(setupWorkspace,
      OWNER, REPO, GH_TOKEN, 'main', 'refactor', TASK_ID, WS, expect.any(Object), 'blueprint', undefined,
    );
  });

  it('includes REFACTOR TASK strict constraints in prompt', async () => {
    const task = { title: 'Refactor task', description: '' };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).toContain('REFACTOR TASK — STRICT CONSTRAINTS');
  });
});

// ── Execution-evidence SR categories ─────────────────────────────────────────
describe('execution-evidence SR categories in prompt', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('includes execution-evidence category instructions in handleCreate prompt', async () => {
    const task = { title: 'Task', description: '' };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).toContain('tests_executed_cleanly');
    expect(prompt).toContain('lint_executed_cleanly');
    expect(prompt).toContain('typecheck_executed_cleanly');
  });

  it('includes execution-evidence category instructions in handleRefactor prompt', async () => {
    const task = { title: 'Refactor task', description: '' };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).toContain('tests_executed_cleanly');
    expect(prompt).toContain('lint_executed_cleanly');
    expect(prompt).toContain('typecheck_executed_cleanly');
  });
});

// ── Viewport infra skip guidance ──────────────────────────────────────────────
describe('viewport infra skip guidance in prompt', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('includes advisory viewport skip guidance in handleCreate prompt', async () => {
    const task = { title: 'Task', description: '' };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).toContain('VIEWPORT_CAPTURE_SKIPPED');
    expect(prompt).toContain('continue to `./pr`');
    expect(VIEWPORT_INFRA_SKIP_GUIDANCE).not.toMatch(/call[\s\S]*\.\/blocked/i);
  });

  it('includes viewport infra skip guidance in handleRefactor prompt', async () => {
    const task = { title: 'Refactor task', description: '' };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).toContain('VIEWPORT_CAPTURE_SKIPPED');
    expect(VIEWPORT_INFRA_SKIP_GUIDANCE).not.toMatch(/call[\s\S]*\.\/blocked/i);
  });
});

// ── ClaudeMd guard: anthropic provider auto-injects CLAUDE.md ─────────────────
describe('claudeMd guard with providerAutoInjectsClaudeMd', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(readClaudeMd).mockReturnValue('Mock CLAUDE.md instructions for testing');
  });

  it('handleCreate omits ## Codebase Instructions when provider auto-injects ClaudeMd', async () => {
    const task = { title: 'Task', description: '' };
    const payload = {
      prompt: 'Do something',
      acceptance_evidence: {
        kind: 'named_test',
        test_path: 'agent/src/handlers/standard.test.ts',
      },
    };
    await handleCreate(TASK_ID, payload, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    // Anthropic provider (default) auto-injects full CLAUDE.md as system prompt,
    // so the truncated user-message copy should be absent.
    expect(prompt).not.toContain('## Codebase Instructions');
    expect(prompt).toContain('## Acceptance Evidence');
  });

  it('handleRefactor omits ## Codebase Instructions when provider auto-injects ClaudeMd', async () => {
    const task = { title: 'Refactor task', description: '' };
    const payload = {
      prompt: 'Improve it',
      acceptance_evidence: {
        kind: 'named_test',
        test_path: 'agent/src/handlers/standard.test.ts',
      },
    };
    await handleRefactor(TASK_ID, payload, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).not.toContain('## Codebase Instructions');
    expect(prompt).toContain('## Acceptance Evidence');
  });
});

// ── Read-only ./fetch provisioning + guidance ─────────────────────────────────
describe('mid-run ./fetch provisioning for create/refactor', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('handleCreate advertises ./fetch guidance and provisions the writer when project_id is set', async () => {
    const task = { title: 'Task', description: '', project_metadata: { project_id: 'p-xyz' } };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).toContain('MID-RUN FETCH (./fetch)');
    // Only the provisioned read-only verbs are advertised.
    expect(prompt).toContain('./fetch direction [id]');
    expect(prompt).toContain('./fetch section <key>');
    expect(prompt).toContain('./fetch playbook <key>');
    // problem-model/gap/message are NOT provisioned for code-change agents.
    expect(prompt).not.toContain('./fetch problem-model');
    expect(prompt).not.toContain('./fetch gap <id>');
    expectCalledWith(writeFetchHelper,
      TASK_ID, 'p-xyz', expect.anything(), { verbs: PLAN_FETCH_VERBS },
    );
  });

  it('handleCreate omits ./fetch guidance and does not provision when project_id is absent', async () => {
    const task = { title: 'Task', description: '' };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).not.toContain('MID-RUN FETCH (./fetch)');
    expect(vi.mocked(writeFetchHelper)).not.toHaveBeenCalled();
  });

  it('handleRefactor advertises ./fetch guidance and provisions the writer when project_id is set', async () => {
    const task = { title: 'Refactor task', description: '', project_metadata: { project_id: 'p-abc' } };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).toContain('MID-RUN FETCH (./fetch)');
    expectCalledWith(writeFetchHelper,
      TASK_ID, 'p-abc', expect.anything(), { verbs: PLAN_FETCH_VERBS },
    );
  });

  it('handleRefactor omits ./fetch guidance and does not provision when project_id is absent', async () => {
    const task = { title: 'Refactor task', description: '' };
    await handleRefactor(TASK_ID, { prompt: 'Improve it' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).not.toContain('MID-RUN FETCH (./fetch)');
    expect(vi.mocked(writeFetchHelper)).not.toHaveBeenCalled();
  });
});

// ── Module interface extraction (scope-conditional) ───────────────────────────
describe('module interface extraction', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(extractModuleInterfaces).mockReturnValue('## Module Interfaces\nfoo\n\n');
  });

  // Default-OFF (2026-07-10 futility bound): absent an explicit project holdback
  // fraction, module_interfaces is suppressed even at medium scope — the discovery
  // function is NOT invoked and the section is absent from the prompt.
  it('does not call extractModuleInterfaces for handleCreate with scope=medium and no holdback config (default-off)', async () => {
    const task = { title: 'Task', description: '', context: { scope: 'medium' } };
    await handleCreate(TASK_ID, { prompt: 'Do it', related_files: ['src/foo.ts'] }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(extractModuleInterfaces)).not.toHaveBeenCalled();
    expect(capturedPrompt()).not.toContain('## Module Interfaces');
  });

  // Opt-in: an explicit holdback fraction < 1.0 (0.0 = always-treatment) re-enables
  // the section — the discovery function is invoked and the section is injected.
  it('calls extractModuleInterfaces for handleCreate with scope=medium when explicitly opted in (fraction 0.0)', async () => {
    const task = {
      title: 'Task', description: '', context: { scope: 'medium' },
      project_metadata: { module_interfaces_holdback_fraction: 0.0 },
    };
    await handleCreate(TASK_ID, { prompt: 'Do it', related_files: ['src/foo.ts'] }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expectCalledWith(extractModuleInterfaces, WS, ['src/foo.ts']);
    expect(capturedPrompt()).toContain('## Module Interfaces');
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

  it('does not call extractModuleInterfaces for handleRefactor with scope=medium and no holdback config (default-off)', async () => {
    const task = { title: 'Refactor', description: '', context: { scope: 'medium' } };
    await handleRefactor(TASK_ID, { prompt: 'Improve it', related_files: ['src/utils.ts'] }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(extractModuleInterfaces)).not.toHaveBeenCalled();
    expect(capturedPrompt()).not.toContain('## Module Interfaces');
  });

  it('calls extractModuleInterfaces for handleRefactor with scope=medium when explicitly opted in (fraction 0.0)', async () => {
    const task = {
      title: 'Refactor', description: '', context: { scope: 'medium' },
      project_metadata: { module_interfaces_holdback_fraction: 0.0 },
    };
    await handleRefactor(TASK_ID, { prompt: 'Improve it', related_files: ['src/utils.ts'] }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expectCalledWith(extractModuleInterfaces, WS, ['src/utils.ts']);
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

  // Default-OFF (2026-07-10 futility bound): suppressed by default at medium scope.
  it('does not call discoverAffectedConsumers for handleCreate with scope=medium and no holdback config (default-off)', async () => {
    const task = { title: 'Task', description: '', context: { scope: 'medium' } };
    await handleCreate(TASK_ID, { prompt: 'Do it', related_files: ['src/foo.ts'] }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(discoverAffectedConsumers)).not.toHaveBeenCalled();
    expect(capturedPrompt()).not.toContain('## Affected Consumers');
  });

  it('calls discoverAffectedConsumers for handleCreate with scope=medium when explicitly opted in (fraction 0.0)', async () => {
    const task = {
      title: 'Task', description: '', context: { scope: 'medium' },
      project_metadata: { affected_consumers_holdback_fraction: 0.0 },
    };
    await handleCreate(TASK_ID, { prompt: 'Do it', related_files: ['src/foo.ts'] }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expectCalledWith(discoverAffectedConsumers, WS, ['src/foo.ts']);
    expect(capturedPrompt()).toContain('## Affected Consumers');
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

  it('does not call discoverAffectedConsumers for handleRefactor with scope=medium and no holdback config (default-off)', async () => {
    const task = { title: 'Refactor', description: '', context: { scope: 'medium' } };
    await handleRefactor(TASK_ID, { prompt: 'Improve it', related_files: ['src/utils.ts'] }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(discoverAffectedConsumers)).not.toHaveBeenCalled();
    expect(capturedPrompt()).not.toContain('## Affected Consumers');
  });

  it('calls discoverAffectedConsumers for handleRefactor with scope=medium when explicitly opted in (fraction 0.0)', async () => {
    const task = {
      title: 'Refactor', description: '', context: { scope: 'medium' },
      project_metadata: { affected_consumers_holdback_fraction: 0.0 },
    };
    await handleRefactor(TASK_ID, { prompt: 'Improve it', related_files: ['src/utils.ts'] }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expectCalledWith(discoverAffectedConsumers, WS, ['src/utils.ts']);
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

  it('caps oversized pre-implementation analysis context injection on fresh-analysis path', async () => {
    const { sendLog } = await import('../index.js');
    vi.clearAllMocks();
    const underCapSentinel = 'UNDER_CAP_SENTINEL';
    const overCapSentinel = 'OVER_CAP_SENTINEL';
    // Build a string that exceeds 16000 chars; place sentinel past the cap
    const analysisResult = underCapSentinel + 'x'.repeat(16000) + overCapSentinel;
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: '', lastResultText: analysisResult,
    });
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).toContain('## Pre-Implementation Analysis');
    expect(prompt).toContain('[Analysis truncated at 16000 chars');
    expect(prompt).not.toContain(overCapSentinel);
    expectCalledWith(sendLog, null, expect.stringContaining('WARNING: analysis result'));
  });

  it('does not truncate or warn for sub-cap analysis on fresh-analysis path', async () => {
    const { sendLog } = await import('../index.js');
    vi.clearAllMocks();
    const analysisResult = 'Short analysis within cap.';
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: '', lastResultText: analysisResult,
    });
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).toContain(analysisResult);
    expect(prompt).not.toContain('[Analysis truncated');
    const warnCalls = vi.mocked(sendLog).mock.calls.filter(
      ([, msg]) => typeof msg === 'string' && msg.includes('WARNING: analysis result'),
    );
    expect(warnCalls).toHaveLength(0);
  });

  it('caps oversized _previous_analysis_plan on the reuse path', async () => {
    const { sendLog } = await import('../index.js');
    vi.clearAllMocks();
    const underCapSentinel = 'UNDER_CAP_SENTINEL';
    const overCapSentinel = 'OVER_CAP_SENTINEL';
    // Build a stored plan that exceeds 16000 chars; sentinel past the cap should be excluded.
    const priorPlan = underCapSentinel + 'x'.repeat(16000) + overCapSentinel;
    const task = {
      title: 'Task',
      description: '',
      context: { scope: 'medium', _previous_analysis_plan: priorPlan },
      project_context: {},
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // Reuse path must NOT fire the analysis Claude Code call.
    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    const prompt = capturedPrompt();
    expect(prompt).toContain('## Pre-Implementation Analysis');
    expect(prompt).toContain('[Analysis truncated at 16000 chars');
    expect(prompt).not.toContain(overCapSentinel);
    expectCalledWith(sendLog, null, expect.stringContaining('WARNING: analysis result'));
  });

  it('does not truncate or warn when _previous_analysis_plan is within cap on reuse path', async () => {
    const { sendLog } = await import('../index.js');
    vi.clearAllMocks();
    const priorPlan = 'Short stored plan within cap.';
    const task = {
      title: 'Task',
      description: '',
      context: { scope: 'medium', _previous_analysis_plan: priorPlan },
      project_context: {},
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    // Reuse path must NOT fire the analysis Claude Code call.
    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    const prompt = capturedPrompt();
    expect(prompt).toContain(priorPlan);
    expect(prompt).not.toContain('[Analysis truncated');
    const warnCalls = vi.mocked(sendLog).mock.calls.filter(
      ([, msg]) => typeof msg === 'string' && msg.includes('WARNING: analysis result'),
    );
    expect(warnCalls).toHaveLength(0);
  });
});

describe('handleDocs configuration', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('uses branchPrefix "docs"', async () => {
    const task = { title: 'Docs task', description: '' };
    await handleDocs(TASK_ID, {}, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expectCalledWith(setupWorkspace,
      OWNER, REPO, GH_TOKEN, 'main', 'docs', TASK_ID, WS, expect.any(Object), 'blueprint', undefined,
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
    expectCalledWith(setupWorkspace,
      OWNER, REPO, GH_TOKEN, 'main', 'task', TASK_ID, WS, expect.any(Object), 'blueprint', undefined,
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
    expectCalledWith(setupWorkspace,
      OWNER, REPO, GH_TOKEN, 'main', 'deps', TASK_ID, WS, expect.any(Object), 'blueprint', undefined,
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

// ── Cache-optimized prompt prefix ─────────────────────────────────────────────
// The v24 cost lever: the automatically-cacheable prompt prefix (Anthropic
// prefix caching bills a cache read at ~10% of base input) is maximized by
// placing ALL stable literal/config guidance in one contiguous leading prefix
// and ALL task-variable content in a contiguous trailing tail. Two builds of
// the same handler for an identical payload but different wall-clock / git HEAD
// must produce a BYTE-IDENTICAL stable prefix. This is a byte-ORDER change:
// every section still ships, only its position moves.
describe('stable prompt prefix is byte-identical across wall-clock and HEAD', () => {
  const STABLE_SECTIONS = {
    claudeMdPrefix: '## Codebase Instructions\nBE CONSISTENT\n\n',
    isRetry: false,
    codeChangeHelperGuidance: '\n\n## Helpers\n./check ./pr open ./done ./fail\n',
    taskTypeFocusBlock: '\n\n## Focus\nfocus-block-text\n',
    taskTypeHint: '\n\nHint: task-type-hint-text\n',
    firstTaskSection: '',
    fetchGuidance: '## MID-RUN FETCH\nfetch-verbs\n',
  } as const;

  function sections(variable: Partial<StandardPromptSections>): StandardPromptSections {
    return {
      ...STABLE_SECTIONS,
      acceptanceEvidenceNotice: '',
      playbookSection: '',
      skillsSection: '',
      projectToolsSection: '',
      retryContext: '',
      provenanceNotice: '',
      corePrompt: 'CORE_TASK_PROMPT',
      reservedRevisionGuidance: '',
      srFeedbackBlock: '',
      promptSuffix: '',
      prTitlePrefixNote: '',
      ...variable,
    };
  }

  it('yields an identical stable prefix when only task-variable content changes (wall-clock/HEAD proxy)', () => {
    // Wall-clock- and HEAD-derived text (relative-day strings, live SHAs) only
    // ever lands in task-variable sections. Two builds differing ONLY in those
    // sections must share a byte-identical stable prefix while the tail differs.
    const build1 = assembleStandardPrompt(sections({
      corePrompt: 'implement X — updated 2 days ago — HEAD abc1234',
      provenanceNotice: '',
    }));
    const build2 = assembleStandardPrompt(sections({
      corePrompt: 'implement X — updated 9 days ago — HEAD def5678',
      provenanceNotice: 'PROVENANCE_DELTA',
    }));
    expect(build1.stablePrefix).toBe(build2.stablePrefix);
    expect(build1.stablePrefix.length).toBeGreaterThan(0);
    expect(build1.variableBody).not.toBe(build2.variableBody);
  });

  it('places every stable guidance constant strictly before any task-variable section', () => {
    const { finalPrompt } = assembleStandardPrompt(sections({
      corePrompt: 'UNIQUE_TASK_VARIABLE_MARKER',
      acceptanceEvidenceNotice: 'ACCEPTANCE_MARKER',
      retryContext: 'RETRY_MARKER',
      srFeedbackBlock: 'SR_FEEDBACK_MARKER',
    }));
    const firstVariableIdx = Math.min(
      ...['UNIQUE_TASK_VARIABLE_MARKER', 'ACCEPTANCE_MARKER', 'RETRY_MARKER', 'SR_FEEDBACK_MARKER']
        .map((m) => finalPrompt.indexOf(m))
        .filter((i) => i >= 0),
    );
    for (const stable of [
      STABLE_SECTIONS.claudeMdPrefix,
      PLAYBOOK_GUIDANCE,
      DEFAULT_TOOL_GUIDANCE,
      VIEWPORT_INFRA_SKIP_GUIDANCE,
      STABLE_SECTIONS.codeChangeHelperGuidance,
      STABLE_SECTIONS.taskTypeFocusBlock,
    ]) {
      const idx = finalPrompt.indexOf(stable);
      expect(idx).toBeGreaterThanOrEqual(0);
      expect(idx).toBeLessThan(firstVariableIdx);
    }
  });

  it('preserves every sent section (byte-order change only, nothing dropped)', () => {
    const s = sections({
      corePrompt: 'CORE',
      acceptanceEvidenceNotice: 'AE',
      playbookSection: 'PB',
      skillsSection: 'SK',
      srFeedbackBlock: 'SR',
      reservedRevisionGuidance: 'RR',
      promptSuffix: 'PS',
      prTitlePrefixNote: 'PT',
    });
    const { finalPrompt } = assembleStandardPrompt(s);
    for (const piece of [
      s.claudeMdPrefix, s.codeChangeHelperGuidance, s.taskTypeFocusBlock, s.taskTypeHint,
      'CORE', 'AE', 'PB', 'SK', 'SR', 'RR', 'PS', 'PT',
      PLAYBOOK_GUIDANCE, DEFAULT_TOOL_GUIDANCE, VIEWPORT_INFRA_SKIP_GUIDANCE,
    ]) {
      expect(finalPrompt).toContain(piece);
    }
  });

  it('handleCreate leads with stable guidance and is byte-identical across wall-clock and HEAD', async () => {
    vi.clearAllMocks();
    const MARKER = 'END_TO_END_TASK_VARIABLE_MARKER_9f3a';
    const task = { title: 'T', description: '', context: {}, project_context: {} };

    // Build 1 — git HEAD "aaaaaaaa", wall-clock T0.
    vi.mocked(git).mockReturnValue('aaaaaaaa');
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(1_700_000_000_000);
    await handleCreate(TASK_ID, { prompt: MARKER }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt1 = capturedPrompt();

    // Build 2 — different git HEAD, wall-clock advanced 30 days.
    vi.mocked(runCodeChangeTask).mockClear();
    vi.mocked(git).mockReturnValue('bbbbbbbb');
    nowSpy.mockReturnValue(1_700_000_000_000 + 30 * 86_400_000);
    await handleCreate(TASK_ID, { prompt: MARKER }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt2 = capturedPrompt();
    nowSpy.mockRestore();

    // The reorder: stable guidance constants precede the task-variable marker.
    const markerIdx1 = prompt1.indexOf(MARKER);
    expect(markerIdx1).toBeGreaterThan(0);
    expect(prompt1.indexOf(PLAYBOOK_GUIDANCE)).toBeGreaterThanOrEqual(0);
    expect(prompt1.indexOf(PLAYBOOK_GUIDANCE)).toBeLessThan(markerIdx1);
    expect(prompt1.indexOf(DEFAULT_TOOL_GUIDANCE)).toBeLessThan(markerIdx1);
    expect(prompt1.indexOf(VIEWPORT_INFRA_SKIP_GUIDANCE)).toBeLessThan(markerIdx1);

    // The stable prefix (everything up to the first task-variable byte) is
    // byte-identical across the two builds despite different wall-clock/HEAD.
    const prefix1 = prompt1.slice(0, markerIdx1);
    const prefix2 = prompt2.slice(0, prompt2.indexOf(MARKER));
    expect(prefix1.length).toBeGreaterThan(0);
    expect(prefix1).toBe(prefix2);
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

  it('calls captureTestBaseline when scope is not set', async () => {
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(vi.mocked(captureTestBaseline)).toHaveBeenCalled();
  });

  it('passes related_files from payload to captureTestBaseline', async () => {
    const relatedFiles = ['agent/src/index.ts', 'agent/src/handlers/standard.ts'];
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something', related_files: relatedFiles }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expectCalledWith(captureTestBaseline,
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
    const { default: fs } = await import('fs');
    vi.mocked(captureTestBaseline).mockResolvedValue(new Set(['test A', 'test B']));
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const baselineCalls = vi.mocked(fs.promises.writeFile).mock.calls.filter(c => (c[0] as string).includes('test_baseline_'));
    expect(baselineCalls).toHaveLength(1);
    expect(baselineCalls[0][0]).toBe(`/tmp/test_baseline_${TASK_ID}.json`);
    expect(baselineCalls[0][1] as string).toContain('test A');
  });

  it('does not write test_baseline file when captureTestBaseline returns null', async () => {
    const { default: fs } = await import('fs');
    vi.mocked(fs.promises.writeFile).mockClear();
    vi.mocked(captureTestBaseline).mockResolvedValue(null);
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const baselineCalls = vi.mocked(fs.promises.writeFile).mock.calls.filter(c => (c[0] as string).includes('test_baseline_'));
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
    expectCalledWith(buildAnalysisPrompt,
      expect.objectContaining({ cochangeFiles: ['x/y.ts', 'p/q.ts'] }),
    );
  });

  it('retries analysis with narrowing guidance when plan exceeds the limit', async () => {
    // 20 files > MEDIUM_ANALYSIS_PLAN_FILE_LIMIT (16).
    const overflowFiles = Array.from({ length: 20 }, (_, i) => `file${i}.ts`);
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
    expectNotCalledWith(persistAndPostStatus,
      TASK_ID,
      expect.objectContaining({ failure_category: 'scope_overflow' }),
    );
  });

  it('fails fast with scope_overflow when narrowing retry still exceeds the limit', async () => {
    // 20 files > MEDIUM_ANALYSIS_PLAN_FILE_LIMIT (16), even after the narrowing retry.
    const overflow = Array.from({ length: 20 }, (_, i) => `file${i}.ts`);
    vi.mocked(parseAnalysisPlanFiles).mockReturnValue(overflow);
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '**Files to change**\n- a.ts' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '**Files to change**\n- a.ts' });
    const task = { title: 'Task', description: '', context: { scope: 'medium' }, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const { persistAndPostStatus } = await import('../index.js');
    expectCalledWith(persistAndPostStatus,
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
    expectNotCalledWith(persistAndPostStatus,
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
    expectNotCalledWith(persistAndPostStatus,
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

const STANDARD_CODE_TASKS = [
  ['create', handleCreate],
  ['refactor', handleRefactor],
  ['docs', handleDocs],
  ['qa', handleQA],
  ['deps', handleDeps],
] as const;

describe('acceptance evidence propagation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it.each(STANDARD_CODE_TASKS)('includes the acceptance-evidence block for %s tasks', async (taskType, handler) => {
    await handler(
      TASK_ID,
      {
        prompt: 'Do something',
        type: taskType,
        acceptance_evidence: {
          kind: 'named_test',
          test_path: 'testing/test_widget.py',
          test_id: 'test_widget_handles_empty',
        },
      },
      { title: 'Task', description: '', context: {}, project_context: {} },
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const prompt = capturedPrompt();
    expect(prompt).toContain('## Acceptance Evidence');
    expect(prompt).toContain('testing/test_widget.py');
    expect(prompt).toContain('test_widget_handles_empty');
    expect(prompt).toContain('real merge-test verdict');
  });

  it.each(STANDARD_CODE_TASKS)('omits the acceptance-evidence block when undeclared for %s tasks', async (_taskType, handler) => {
    await handler(
      TASK_ID,
      { prompt: 'Do something', type: _taskType },
      { title: 'Task', description: '', context: {}, project_context: {} },
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    expect(capturedPrompt()).not.toContain('## Acceptance Evidence');
  });
});

describe('commissioned artifact prerequisite injection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('injects artifact prerequisites without pre-filling consumed ids', async () => {
    const fs = (await import('fs')).default;
    const task = {
      title: 'Task',
      description: '',
      context: {
        artifact_prerequisites: [
          {
            artifact_id: 'artifact-1',
            logical_key: 'release-promotion-record',
            artifact_type: 'COMPUTE_RESULT',
            source_task_id: 'producer-1',
          },
        ],
      },
      project_context: {},
    };

    await handleCreate(TASK_ID, { prompt: 'Use the artifact' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const prompt = capturedPrompt();
    expect(prompt).toContain('## Commissioned Prerequisite Artifacts');
    expect(prompt).toContain('artifact_id: artifact-1');
    expect(prompt).toContain('only record artifact ids you truly consumed');
    expect(prompt).toContain(`/tmp/consumed_artifacts_${TASK_ID}.json`);
    expect(vi.mocked(fs.writeFileSync)).not.toHaveBeenCalledWith(
      `/tmp/consumed_artifacts_${TASK_ID}.json`,
      expect.anything(),
      expect.anything(),
    );
  });

  it('hydrates compute results before provider launch and prompts only with fixed local paths', async () => {
    vi.mocked(fetchArtifactDownloadUrl).mockResolvedValueOnce(
      'https://signed.example/secret-bundle',
    );
    vi.mocked(hydrateComputeResultArtifacts).mockResolvedValueOnce({
      localPaths: new Map([
        [
          'artifact-compute',
          '.blueprint-inputs/compute-jobs/job-canonical',
        ],
      ]),
      rejectedEntries: [],
    });
    const task = {
      title: 'Task',
      description: '',
      context: {
        artifact_prerequisites: [
          {
            artifact_id: 'artifact-compute',
            artifact_type: 'COMPUTE_RESULT',
            source_task_id: 'producer-1',
            source_compute_job_id: 'job-canonical',
            storage_uri: (
              'gs://blueprint-agent-artifacts/compute-jobs/job-other/'
              + 'artifacts/transfers/bundle.tar.gz'
            ),
          },
        ],
      },
      project_context: {},
    };

    await handleCreate(
      TASK_ID,
      { prompt: 'Use the compute evidence' },
      task,
      GH_TOKEN,
      OWNER,
      REPO,
      WS,
      STATUS_REF(),
    );

    expect(hydrateComputeResultArtifacts).toHaveBeenCalledWith(
      expect.objectContaining({
        pointers: [
          expect.objectContaining({
            artifact_id: 'artifact-compute',
            source_compute_job_id: 'job-canonical',
          }),
        ],
      }),
    );
    expect(
      vi.mocked(hydrateComputeResultArtifacts).mock.invocationCallOrder[0],
    ).toBeLessThan(vi.mocked(runCodeChangeTask).mock.invocationCallOrder[0]);
    const prompt = capturedPrompt();
    expect(prompt).toContain(
      '.blueprint-inputs/compute-jobs/job-canonical/bundle.tar.gz',
    );
    expect(prompt).toContain(
      '.blueprint-inputs/compute-jobs/job-canonical/contents/',
    );
    expect(prompt).not.toContain('https://signed.example/secret-bundle');
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
    expectCalledWith(idx.persistAndPostStatus,
      TASK_ID,
      expect.objectContaining({
        status: 'COMPLETED',
        artifact_type: 'PULL_REQUEST',
        pr_url: prUrl,
        changed_files: ['src/foo.ts', 'src/bar.ts'],
      }),
    );
  });

  it('posts FAILED with failure_category="incomplete" when /tmp/pr_url_<id> contains a non-pull URL (compare URL improvised by Claude)', async () => {
    // Production incident 2026-05-06: a haiku retry whose ./check viewport
    // gate was wedged on a missing libatk system library improvised by
    // writing a `https://github.com/.../compare/main...blueprint/task-...`
    // URL into /tmp/pr_url_<id>. The wrapper trusted the file uncritically;
    // the resulting PULL_REQUEST artifact then crashed downstream merge
    // verification 36×/cycle. The wrapper now shape-validates and falls
    // through to the standard FAILED path.
    const fs = (await import('fs')).default;
    const idx = await import('../index.js');
    const compareUrl = 'https://github.com/owner/repo/compare/main...blueprint/task-cb7bb07e';
    vi.mocked(fs.existsSync).mockImplementation((p: unknown) => p === `/tmp/pr_url_${TASK_ID}`);
    vi.mocked(fs.readFileSync).mockReturnValue(compareUrl);
    vi.mocked(idx.git).mockImplementation((args: readonly string[]) => {
      if (args[0] === 'status') return '';
      if (args[0] === 'rev-parse') return 'basecommit';
      return '';
    });
    vi.mocked(idx.runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    await realRunCodeChangeTask(baseOpts());
    const calls = vi.mocked(idx.persistAndPostStatus).mock.calls;
    expect(calls).toHaveLength(1);
    expect(calls[0][1]).toEqual(expect.objectContaining({
      status: 'FAILED',
      failure_category: 'incomplete',
    }));
    // Crucially, no COMPLETED with the bogus URL.
    expect(calls[0][1]).not.toEqual(expect.objectContaining({ pr_url: compareUrl }));
  });

  it('posts FAILED when /tmp/pr_url_<id> contains arbitrary non-URL text', async () => {
    const fs = (await import('fs')).default;
    const idx = await import('../index.js');
    vi.mocked(fs.existsSync).mockImplementation((p: unknown) => p === `/tmp/pr_url_${TASK_ID}`);
    vi.mocked(fs.readFileSync).mockReturnValue('PR pending — see logs');
    vi.mocked(idx.git).mockImplementation((args: readonly string[]) => {
      if (args[0] === 'status') return '';
      if (args[0] === 'rev-parse') return 'basecommit';
      return '';
    });
    vi.mocked(idx.runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    await realRunCodeChangeTask(baseOpts());
    expectCalledWith(idx.persistAndPostStatus,
      TASK_ID,
      expect.objectContaining({ status: 'FAILED', failure_category: 'incomplete' }),
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
    expectCalledWith(idx.persistAndPostStatus,
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
    expectCalledWith(idx.persistAndPostStatus,
      TASK_ID,
      expect.objectContaining({
        status: 'FAILED',
        failure_category: 'claude_code_failed',
      }),
    );
  });

  it('FAILS as incomplete (NOT a silent COMPLETED) on a bare no-diff clean exit even when noChangesMessage is set', async () => {
    // SAFEGUARD (2026-06-01): the passive "no diff ⇒ COMPLETED" rule was removed.
    // A no-op completion now requires the affirmative ./done --already-done verb
    // (which writes /tmp/done_<id>). Without that marker, a no-diff exit is
    // `incomplete` so a give-up/error cannot masquerade as "nothing to do".
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
    expectCalledWith(idx.persistAndPostStatus,
      TASK_ID,
      expect.objectContaining({
        status: 'FAILED',
        failure_category: 'incomplete',
      }),
    );
    // It must NOT silently complete with the no-changes message.
    const completed = vi.mocked(idx.persistAndPostStatus).mock.calls.filter(
      c => c[1].status === 'COMPLETED',
    );
    expect(completed).toHaveLength(0);
  });

  it('exits cleanly (no terminal post) when ./done --already-done wrote the done marker', async () => {
    // Affirmative no-op completion: ./done --already-done already posted
    // COMPLETED (no PR) and wrote /tmp/done_<id>; the wrapper just exits.
    const fs = (await import('fs')).default;
    const idx = await import('../index.js');
    vi.mocked(fs.existsSync).mockImplementation((p) =>
      String(p) === `/tmp/done_${TASK_ID}`,
    );
    vi.mocked(idx.runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    await realRunCodeChangeTask({ ...baseOpts(), noChangesMessage: 'No documentation changes needed.' });
    expect(vi.mocked(idx.persistAndPostStatus)).not.toHaveBeenCalled();
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
    expect(p).not.toContain("If You're Retrying");                       // PRIOR_ATTEMPT_DIVERGENCE omitted on first attempt
    expect(p).toContain('When You Find Bigger Problems Than Your Task');  // BEYOND_SCOPE_REPORTING
    expect(p).toContain('Use Grep/Glob for code search');                // DEFAULT_TOOL_GUIDANCE
  });

  it('handleCreate prompt includes PRIOR_ATTEMPT_DIVERGENCE on retry', async () => {
    const RETRY_TASK = {
      ...TASK,
      context: { _attempt_number: 2, _max_attempts: 3, _previous_error: 'test error' },
    };
    await handleCreate(TASK_ID, PAYLOAD, RETRY_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const p = capturedPrompt();
    expect(p).toContain("If You're Retrying");                           // PRIOR_ATTEMPT_DIVERGENCE included on retry
  });

  it('handleRefactor prompt includes required code-change guidance blocks', async () => {
    await handleRefactor(TASK_ID, PAYLOAD, TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const p = capturedPrompt();
    expect(p).toContain('Task lifecycle — you own it end-to-end');
    expect(p).toContain('Use Grep/Glob for code search');
  });

  it('build_config task gets project-aware ./check guidance (no Blueprint stacks, keeps lifecycle)', async () => {
    // Non-Blueprint owner/repo (owner/repo) + a configured build_config → the
    // ./check guidance must describe the project's commands, not Blueprint's.
    const BC_TASK = {
      ...TASK,
      build_config: {
        directories: [
          { path: 'web', type: 'node', lint_cmd: 'eslint src', build_cmd: 'npm run build', test_cmd: 'npm test' },
        ],
      },
    };
    await handleCreate(TASK_ID, PAYLOAD, BC_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const p = capturedPrompt();
    // repo-agnostic lifecycle survives
    expect(p).toContain('Task lifecycle — you own it end-to-end');
    expect(p).toContain('./pr open');
    expect(p).toContain('./done');
    // Blueprint-only ./check stack identity is gone
    expect(p).not.toContain('scripts/test-backend.sh');
    expect(p).not.toContain('backend ruff');
    expect(p).not.toContain('agent tsc');
    // project's own configured commands are referenced
    expect(p).toContain('npm run build');
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

// ── Pre-execution FP-rate loosen hint wiring ─────────────────────────────────
describe('pre-execution FP-rate loosen hint in prompt', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('emits loosen hint when a category has fp_rate >= 0.5 and sample_size >= 10', async () => {
    const task = {
      title: 'T', description: '', context: {},
      project_context: {
        self_review_catch_rate: {
          categories: {
            correctness: { false_positive_rate: 0.7, sr_flag_sample_size: 15 },
          },
        },
      },
    };
    await handleCreate(TASK_ID, { prompt: 'do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const p = capturedPrompt();
    expect(p).toContain('Over-flagged categories');
    expect(p).toContain('correctness');
    expect(p).toContain('70% false-positive rate');
  });

  it('omits loosen hint when fp_rate is below 0.5', async () => {
    const task = {
      title: 'T', description: '', context: {},
      project_context: {
        self_review_catch_rate: {
          categories: {
            correctness: { false_positive_rate: 0.4, sr_flag_sample_size: 20 },
          },
        },
      },
    };
    await handleCreate(TASK_ID, { prompt: 'do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).not.toContain('Over-flagged categories');
  });

  it('omits loosen hint when sample_size is below 10', async () => {
    const task = {
      title: 'T', description: '', context: {},
      project_context: {
        self_review_catch_rate: {
          categories: {
            correctness: { false_positive_rate: 0.8, sr_flag_sample_size: 9 },
          },
        },
      },
    };
    await handleCreate(TASK_ID, { prompt: 'do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).not.toContain('Over-flagged categories');
  });

  it('omits loosen hint when project_context has no self_review_catch_rate', async () => {
    const task = { title: 'T', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).not.toContain('Over-flagged categories');
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

  it('includes create focus block and generic guidance for create task type', async () => {
    await handleCreate(TASK_ID, { prompt: 'do something', type: 'create' }, TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const p = capturedPrompt();
    expect(p).toContain('Self-review focus: create tasks');
    expect(p).toContain('Task lifecycle — you own it end-to-end');
  });

  it('does not include any focus block when task type is absent', async () => {
    await handleCreate(TASK_ID, { prompt: 'do something' }, TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expect(capturedPrompt()).not.toContain('Self-review focus:');
  });
});

// ── computeRegressionRiskHeuristic ───────────────────────────────────────────
describe('computeRegressionRiskHeuristic', () => {
  it('returns passed=true for empty diff', () => {
    const { passed, issue } = computeRegressionRiskHeuristic('');
    expect(passed).toBe(true);
    expect(issue).toContain('[heuristic-derived]');
  });

  it('flags high source-to-test ratio (>=80 src, 0 test lines)', () => {
    const srcLines = Array(85).fill('+const x = 1;').join('\n');
    const diff = `diff --git a/src/foo.ts b/src/foo.ts\n--- a/src/foo.ts\n+++ b/src/foo.ts\n${srcLines}`;
    const { passed, issue } = computeRegressionRiskHeuristic(diff);
    expect(passed).toBe(false);
    expect(issue).toContain('source lines');
    expect(issue).toContain('[heuristic-derived]');
  });

  it('does not flag ratio when test lines accompany source changes', () => {
    const srcLines = Array(85).fill('+const x = 1;').join('\n');
    const testLines = Array(40).fill('+expect(x).toBe(1);').join('\n');
    const diff = [
      'diff --git a/src/foo.ts b/src/foo.ts',
      '--- a/src/foo.ts',
      '+++ b/src/foo.ts',
      srcLines,
      'diff --git a/src/foo.test.ts b/src/foo.test.ts',
      '--- a/src/foo.test.ts',
      '+++ b/src/foo.test.ts',
      testLines,
    ].join('\n');
    const { passed } = computeRegressionRiskHeuristic(diff);
    expect(passed).toBe(true);
  });

  it('flags removed exported function', () => {
    const diff = [
      'diff --git a/src/api.ts b/src/api.ts',
      '--- a/src/api.ts',
      '+++ b/src/api.ts',
      '-export function handleRequest(req: Request) {}',
    ].join('\n');
    const { passed, issue } = computeRegressionRiskHeuristic(diff);
    expect(passed).toBe(false);
    expect(issue).toContain('export');
    expect(issue).toContain('[heuristic-derived]');
  });

  it('does not flag an added export as a removal', () => {
    const diff = [
      'diff --git a/src/api.ts b/src/api.ts',
      '+export function newHelper() {}',
    ].join('\n');
    const { passed } = computeRegressionRiskHeuristic(diff);
    expect(passed).toBe(true);
  });
});

// ── SCOPE_EMPTY pre-flight guard ──────────────────────────────────────────────
// When an auto-decomposed cluster task's declared related_files are fully
// covered by completed sibling tasks, the handler should emit SCOPE_EMPTY
// and return early (no LLM call, no retry budget consumed).
describe('handleCreate — SCOPE_EMPTY pre-flight guard', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('exits with SCOPE_EMPTY when all related_files are covered by completed siblings', async () => {
    const idx = await import('../index.js');
    const task = {
      title: 'Cluster 2/3',
      description: '',
      context: {
        auto_decomposed: true,
        related_files: ['agent/src/handlers/standard.ts'],
      },
      project_context: {
        sibling_tasks: [
          {
            status: 'COMPLETED',
            title: 'Cluster 1/3',
            files: ['agent/src/handlers/standard.ts'],
          },
        ],
      },
    };
    await handleCreate(
      TASK_ID,
      { prompt: 'Do something', related_files: ['agent/src/handlers/standard.ts'] },
      task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    expect(vi.mocked(runCodeChangeTask)).not.toHaveBeenCalled();
    expectCalledWith(idx.persistAndPostStatus,
      TASK_ID,
      expect.objectContaining({
        status: 'BLOCKED_PRECONDITION',
        failure_category: 'SCOPE_EMPTY',
      }),
    );
    const call = vi.mocked(idx.persistAndPostStatus).mock.calls.find(
      ([, body]) => body.failure_category === 'SCOPE_EMPTY',
    );
    expect(call?.[1].logs).toContain('agent/src/handlers/standard.ts');
    expect(call?.[1].logs).toContain('Cluster 1/3');
  });

  it('proceeds normally when some related_files are NOT covered by completed siblings', async () => {
    const task = {
      title: 'Cluster 2/3',
      description: '',
      context: {
        auto_decomposed: true,
        related_files: ['agent/src/handlers/standard.ts', 'agent/src/handlers/code-change.ts'],
      },
      project_context: {
        sibling_tasks: [
          {
            status: 'COMPLETED',
            title: 'Cluster 1/3',
            files: ['agent/src/handlers/standard.ts'],
          },
        ],
      },
    };
    await handleCreate(
      TASK_ID,
      { prompt: 'Do something', related_files: ['agent/src/handlers/standard.ts', 'agent/src/handlers/code-change.ts'] },
      task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    expect(vi.mocked(runCodeChangeTask)).toHaveBeenCalled();
  });

  it('proceeds normally when auto_decomposed is false even if sibling files match', async () => {
    const task = {
      title: 'Regular task',
      description: '',
      context: {
        auto_decomposed: false,
        related_files: ['agent/src/handlers/standard.ts'],
      },
      project_context: {
        sibling_tasks: [
          {
            status: 'COMPLETED',
            title: 'Sibling',
            files: ['agent/src/handlers/standard.ts'],
          },
        ],
      },
    };
    await handleCreate(
      TASK_ID,
      { prompt: 'Do something', related_files: ['agent/src/handlers/standard.ts'] },
      task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    expect(vi.mocked(runCodeChangeTask)).toHaveBeenCalled();
  });

  it('proceeds normally when sibling tasks list is empty', async () => {
    const task = {
      title: 'Cluster 2/3',
      description: '',
      context: {
        auto_decomposed: true,
        related_files: ['agent/src/handlers/standard.ts'],
      },
      project_context: { sibling_tasks: [] },
    };
    await handleCreate(
      TASK_ID,
      { prompt: 'Do something', related_files: ['agent/src/handlers/standard.ts'] },
      task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    expect(vi.mocked(runCodeChangeTask)).toHaveBeenCalled();
  });

  it('proceeds normally when no siblings are in COMPLETED status', async () => {
    const task = {
      title: 'Cluster 2/3',
      description: '',
      context: {
        auto_decomposed: true,
        related_files: ['agent/src/handlers/standard.ts'],
      },
      project_context: {
        sibling_tasks: [
          {
            status: 'IN_PROGRESS',
            title: 'Cluster 1/3',
            files: ['agent/src/handlers/standard.ts'],
          },
        ],
      },
    };
    await handleCreate(
      TASK_ID,
      { prompt: 'Do something', related_files: ['agent/src/handlers/standard.ts'] },
      task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    expect(vi.mocked(runCodeChangeTask)).toHaveBeenCalled();
  });
});

// ── Repo-map arm assignment heartbeat ────────────────────────────────────────
// TASK_ID = 'task-123' arm assignments under the salted djb2 hash ('repo_map:task-123'):
//   salted hash ≈ 0.089
//   fraction=0.05 → 0.089 >= 0.05 → treatment arm (repoMapArm=true)
//   fraction=0.5  → 0.089 <  0.5  → holdback arm  (repoMapArm=false)
//   fraction=1.0  → 0.089 <  1.0  → holdback arm  (repoMapArm=false)

/** Extract the sendHeartbeatSync options dict that contains had_repo_map. */
function capturedRepoMapHeartbeatOpts(): Record<string, unknown> | undefined {
  const calls = vi.mocked(sendHeartbeatSync).mock.calls;
  const call = calls.find(([, , opts]) => opts != null && typeof opts === 'object' && 'had_repo_map' in opts);
  return call ? call[2] : undefined;
}

describe('receipt-only capability trial arm assignment', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(buildRepoMap).mockReturnValue('');
    vi.mocked(extractModuleInterfaces).mockReturnValue('MODULE_INTERFACE_CONTEXT');
    vi.mocked(discoverRelatedTests).mockReturnValue('RELATED_TEST_CONTEXT');
    vi.mocked(discoverAffectedConsumers).mockReturnValue('AFFECTED_CONSUMERS_CONTEXT');
  });

  it('assigns independent salted arms for each capability', () => {
    expect(assignCapabilityHoldbackArm(TASK_ID, 'module_interfaces', 0.5)).toBe(false);
    expect(assignCapabilityHoldbackArm(TASK_ID, 'related_tests', 0.5)).toBe(true);
    expect(assignCapabilityHoldbackArm(TASK_ID, 'affected_consumers', 0.5)).toBe(true);
  });

  it('repo_map and diagnostics_after_edit arms diverge — computed from distinct salted hashes', () => {
    // If both trials used the same unsalted hash (the pre-fix bug), every task
    // would get identical assignments and this loop would never find divergence.
    // Finding at least one task where the arms differ proves they are computed
    // from independent salted inputs (capability:taskId).
    let foundDivergence = false;
    for (let i = 0; i < 200; i++) {
      const taskId = `task-salt-${i}`;
      const repoMap = assignCapabilityHoldbackArm(taskId, 'repo_map', 0.5);
      const diag = assignCapabilityHoldbackArm(taskId, 'diagnostics_after_edit', 0.5);
      if (repoMap !== diag) {
        foundDivergence = true;
        break;
      }
    }
    expect(foundDivergence).toBe(true);
  });

  it('suppresses only held-back capability sections and emits independent create-task arms', async () => {
    const task = {
      title: 'Task',
      description: '',
      context: { scope: 'medium' },
      project_context: {},
      project_metadata: {
        module_interfaces_holdback_fraction: 0.5,
        related_tests_holdback_fraction: 0.5,
        affected_consumers_holdback_fraction: 0.5,
      },
    };

    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const prompt = capturedPrompt();
    expect(prompt).not.toContain('MODULE_INTERFACE_CONTEXT');
    expect(prompt).toContain('RELATED_TEST_CONTEXT');
    expect(prompt).toContain('AFFECTED_CONSUMERS_CONTEXT');
    const opts = capturedRepoMapHeartbeatOpts();
    expect(opts).toMatchObject({
      module_interfaces_arm: false,
      related_tests_arm: true,
      affected_consumers_arm: true,
      had_module_interfaces: false,
      had_related_tests: true,
      had_affected_consumers: true,
    });
  });

  it('suppresses held-back refactor sections before prompt assembly', async () => {
    // related_tests explicitly held back (1.0); module_interfaces + affected_consumers
    // explicitly opted back in (0.0) — proves the arm is per-capability, only the
    // held-back section is suppressed. (Under default-off, the other two must be
    // explicitly opted in to remain injected.)
    const task = {
      title: 'Task',
      description: '',
      context: { scope: 'medium' },
      project_context: {},
      project_metadata: {
        related_tests_holdback_fraction: 1.0,
        module_interfaces_holdback_fraction: 0.0,
        affected_consumers_holdback_fraction: 0.0,
      },
    };

    await handleRefactor(TASK_ID, { prompt: 'Refactor this' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const prompt = capturedPrompt();
    expect(prompt).toContain('MODULE_INTERFACE_CONTEXT');
    expect(prompt).not.toContain('RELATED_TEST_CONTEXT');
    expect(prompt).toContain('AFFECTED_CONSUMERS_CONTEXT');
    const opts = capturedRepoMapHeartbeatOpts();
    expect(opts?.related_tests_arm).toBe(false);
    expect(opts?.had_related_tests).toBe(false);
  });

  it('omits capability arm flags when the trial fraction is not running', async () => {
    const task = {
      title: 'Task',
      description: '',
      context: { scope: 'medium' },
      project_context: {},
      project_metadata: {
        module_interfaces_holdback_fraction: 0.0,
        related_tests_holdback_fraction: 0.0,
        affected_consumers_holdback_fraction: 0.0,
      },
    };

    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const opts = capturedRepoMapHeartbeatOpts();
    expect(opts).toMatchObject({
      had_module_interfaces: true,
      had_related_tests: true,
      had_affected_consumers: true,
    });
    expect(opts?.module_interfaces_arm).toBeUndefined();
    expect(opts?.related_tests_arm).toBeUndefined();
    expect(opts?.affected_consumers_arm).toBeUndefined();
  });

  it('excludes small-scope module and consumer flags while keeping related-tests trial signals', async () => {
    const task = {
      title: 'Task',
      description: '',
      context: { scope: 'small' },
      project_context: {},
      project_metadata: {
        module_interfaces_holdback_fraction: 1.0,
        related_tests_holdback_fraction: 0.5,
        affected_consumers_holdback_fraction: 1.0,
      },
    };

    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const opts = capturedRepoMapHeartbeatOpts();
    expect(opts?.had_module_interfaces).toBeUndefined();
    expect(opts?.module_interfaces_arm).toBeUndefined();
    expect(opts?.had_affected_consumers).toBeUndefined();
    expect(opts?.affected_consumers_arm).toBeUndefined();
    expect(opts?.had_related_tests).toBe(true);
    expect(opts?.related_tests_arm).toBe(true);

    vi.clearAllMocks();
    vi.mocked(buildRepoMap).mockReturnValue('');
    vi.mocked(extractModuleInterfaces).mockReturnValue('MODULE_INTERFACE_CONTEXT');
    vi.mocked(discoverRelatedTests).mockReturnValue('RELATED_TEST_CONTEXT');
    vi.mocked(discoverAffectedConsumers).mockReturnValue('AFFECTED_CONSUMERS_CONTEXT');

    await handleRefactor(TASK_ID, { prompt: 'Refactor this' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const refactorOpts = capturedRepoMapHeartbeatOpts();
    expect(refactorOpts?.had_module_interfaces).toBeUndefined();
    expect(refactorOpts?.module_interfaces_arm).toBeUndefined();
    expect(refactorOpts?.had_affected_consumers).toBeUndefined();
    expect(refactorOpts?.affected_consumers_arm).toBeUndefined();
    expect(refactorOpts?.had_related_tests).toBe(true);
    expect(refactorOpts?.related_tests_arm).toBe(true);
  });
});

// ── Default-OFF: all five futility-proven context blocks suppressed absent config ─
// (2026-07-10 futility bound: repo_map, related_tests, module_interfaces,
// affected_consumers, diagnostics_after_edit all evaluated no_effect.) With no
// project holdback config, none of the five discovery functions is invoked (saving
// compute) and every section is absent (saving tokens), while the heartbeat honestly
// reports the suppression: had_*=false and *_arm=false.
describe('futility-proven context blocks default-off (no project holdback config)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getProviderRunner).mockReturnValue({ displayName: 'Claude Code', provider: 'anthropic' } as ReturnType<typeof getProviderRunner>);
    vi.mocked(writeDiagnosticsAfterEditHook).mockReturnValue(true);
    // Non-empty discovery results so any accidental invocation would surface as
    // injected sections / had_*=true — the test proves they are NOT called.
    vi.mocked(buildRepoMap).mockReturnValue('REPO_MAP_CONTENT');
    vi.mocked(extractModuleInterfaces).mockReturnValue('MODULE_INTERFACE_CONTEXT');
    vi.mocked(discoverRelatedTests).mockReturnValue('RELATED_TEST_CONTEXT');
    vi.mocked(discoverAffectedConsumers).mockReturnValue('AFFECTED_CONSUMERS_CONTEXT');
  });

  it('suppresses all five sections and skips their discovery functions for a medium-scope Blueprint self-repo create task', async () => {
    // scope=medium → module_interfaces/affected_consumers are scope-eligible;
    // anthropic + Blueprint self-repo → diagnostics_after_edit is capability-eligible.
    // No *_holdback_fraction keys → every capability falls to the default-off (1.0) arm.
    const task = {
      title: 'Task',
      description: '',
      context: { scope: 'medium' },
      project_context: {},
      project_metadata: { diagnostics_after_edit_enabled: true },
    };

    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, 'HenryHamster', 'BlueprintAIStudio', WS, STATUS_REF());

    // Discovery functions are not invoked (compute saved).
    expect(vi.mocked(buildRepoMap)).not.toHaveBeenCalled();
    expect(vi.mocked(extractModuleInterfaces)).not.toHaveBeenCalled();
    expect(vi.mocked(discoverRelatedTests)).not.toHaveBeenCalled();
    expect(vi.mocked(discoverAffectedConsumers)).not.toHaveBeenCalled();
    expect(vi.mocked(writeDiagnosticsAfterEditHook)).not.toHaveBeenCalled();

    // No section content is injected into the prompt (tokens saved).
    const prompt = capturedPrompt();
    expect(prompt).not.toContain('REPO_MAP_CONTENT');
    expect(prompt).not.toContain('MODULE_INTERFACE_CONTEXT');
    expect(prompt).not.toContain('RELATED_TEST_CONTEXT');
    expect(prompt).not.toContain('AFFECTED_CONSUMERS_CONTEXT');

    // Heartbeat honestly reflects suppression: arm=false, receipt (had_*)=false.
    const opts = capturedRepoMapHeartbeatOpts();
    expect(opts).toBeDefined();
    expect(opts?.had_repo_map).toBe(false);
    expect(opts?.repo_map_arm).toBe(false);
    expect(opts?.had_module_interfaces).toBe(false);
    expect(opts?.module_interfaces_arm).toBe(false);
    expect(opts?.had_related_tests).toBe(false);
    expect(opts?.related_tests_arm).toBe(false);
    expect(opts?.had_affected_consumers).toBe(false);
    expect(opts?.affected_consumers_arm).toBe(false);
    expect(opts?.diagnostics_after_edit_arm).toBe(false);
    expect(opts?.had_diagnostics_after_edit).toBeUndefined();
  });
});

describe('repo-map arm assignment in sendHeartbeatSync', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getProviderRunner).mockReturnValue({ displayName: 'Claude Code', provider: 'anthropic' } as ReturnType<typeof getProviderRunner>);
  });

  it('sends repo_map_arm=false and had_repo_map=false when holdback arm assigned with empty buildRepoMap', async () => {
    vi.mocked(buildRepoMap).mockReturnValue('');
    const task = {
      title: 'Task',
      description: '',
      context: {},
      project_context: {},
      project_metadata: { repo_map_holdback_fraction: 1.0 },
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const opts = capturedRepoMapHeartbeatOpts();
    expect(opts).toBeDefined();
    expect(opts?.had_repo_map).toBe(false);
    expect(opts?.repo_map_arm).toBe(false);
  });

  it('sends repo_map_arm=true and had_repo_map=true when treatment arm with non-empty repo map', async () => {
    vi.mocked(buildRepoMap).mockReturnValue('some-repo-map-content');
    const task = {
      title: 'Task',
      description: '',
      context: {},
      project_context: {},
      // salted hash('repo_map:task-123') ≈ 0.089; fraction=0.05 → 0.089 >= 0.05 → treatment
      project_metadata: { repo_map_holdback_fraction: 0.05 },
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const opts = capturedRepoMapHeartbeatOpts();
    expect(opts).toBeDefined();
    expect(opts?.had_repo_map).toBe(true);
    expect(opts?.repo_map_arm).toBe(true);
  });

  it('sends repo_map_arm=true and had_repo_map=false when treatment arm with empty buildRepoMap result', async () => {
    // Treatment arm assigned (salted hash('repo_map:task-123') ≈ 0.089 >= fraction 0.05) but buildRepoMap returns ''
    vi.mocked(buildRepoMap).mockReturnValue('');
    const task = {
      title: 'Task',
      description: '',
      context: {},
      project_context: {},
      project_metadata: { repo_map_holdback_fraction: 0.05 },
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const opts = capturedRepoMapHeartbeatOpts();
    expect(opts).toBeDefined();
    expect(opts?.had_repo_map).toBe(false);
    expect(opts?.repo_map_arm).toBe(true);
  });

  it('omits repo_map_arm when holdbackFraction is 0 (no trial running)', async () => {
    vi.mocked(buildRepoMap).mockReturnValue('');
    const task = {
      title: 'Task',
      description: '',
      context: {},
      project_context: {},
      project_metadata: { repo_map_holdback_fraction: 0.0 },
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const opts = capturedRepoMapHeartbeatOpts();
    expect(opts).toBeDefined();
    expect(opts?.had_repo_map).toBe(false);
    expect(opts?.repo_map_arm).toBeUndefined();
  });
});

// ── Diagnostics-after-edit arm assignment heartbeat ─────────────────────────
function capturedDiagnosticsHeartbeatOpts(): Record<string, unknown> | undefined {
  const calls = vi.mocked(sendHeartbeatSync).mock.calls;
  const call = calls.find(([, , opts]) => opts != null && typeof opts === 'object' && (
    'diagnostics_after_edit_arm' in opts || 'had_diagnostics_after_edit' in opts
  ));
  return call ? call[2] : undefined;
}

describe('diagnostics-after-edit arm assignment in sendHeartbeatSync', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getProviderRunner).mockReturnValue({ displayName: 'Claude Code', provider: 'anthropic' } as ReturnType<typeof getProviderRunner>);
    vi.mocked(writeDiagnosticsAfterEditHook).mockReturnValue(true);
  });

  it('materializes the hook and sends assignment plus receipt for an eligible treatment-arm Claude self-repo task', async () => {
    const task = {
      title: 'Task',
      description: '',
      context: {},
      project_context: {},
      project_metadata: {
        diagnostics_after_edit_holdback_fraction: 0.5,
        diagnostics_after_edit_enabled: true,
      },
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, 'HenryHamster', 'BlueprintAIStudio', WS, STATUS_REF());

    expect(writeDiagnosticsAfterEditHook).toHaveBeenCalledTimes(1);
    const opts = capturedDiagnosticsHeartbeatOpts();
    expect(opts?.diagnostics_after_edit_arm).toBe(true);
    expect(opts?.had_diagnostics_after_edit).toBe(true);
  });

  it('sends assignment only and writes no hook for a held-back Claude self-repo task', async () => {
    const task = {
      title: 'Task',
      description: '',
      context: {},
      project_context: {},
      project_metadata: {
        diagnostics_after_edit_holdback_fraction: 1.0,
        diagnostics_after_edit_enabled: true,
      },
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, 'HenryHamster', 'BlueprintAIStudio', WS, STATUS_REF());

    expect(writeDiagnosticsAfterEditHook).not.toHaveBeenCalled();
    const opts = capturedDiagnosticsHeartbeatOpts();
    expect(opts?.diagnostics_after_edit_arm).toBe(false);
    expect(opts?.had_diagnostics_after_edit).toBeUndefined();
  });

  it('sends assignment with a false receipt when the kill switch disables a treatment-arm task', async () => {
    const task = {
      title: 'Task',
      description: '',
      context: {},
      project_context: {},
      project_metadata: {
        diagnostics_after_edit_holdback_fraction: 0.5,
        diagnostics_after_edit_enabled: false,
      },
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, 'HenryHamster', 'BlueprintAIStudio', WS, STATUS_REF());

    expect(writeDiagnosticsAfterEditHook).not.toHaveBeenCalled();
    const opts = capturedDiagnosticsHeartbeatOpts();
    expect(opts?.diagnostics_after_edit_arm).toBe(true);
    expect(opts?.had_diagnostics_after_edit).toBe(false);
  });

  it('materializes the hook and sends a true receipt with no arm key when holdbackFraction is 0 (always-on, no trial running)', async () => {
    // Graduation case: fraction 0.0 means "always on" per the sibling capabilities'
    // `!== false` idiom — there is no randomization to record, so no arm key is
    // fabricated, but the hook must still be materialized (this is the bug: it
    // used to sit behind `diagnosticsHoldbackFraction > 0` and never fire at 0).
    const task = {
      title: 'Task',
      description: '',
      context: {},
      project_context: {},
      project_metadata: {
        diagnostics_after_edit_holdback_fraction: 0.0,
        diagnostics_after_edit_enabled: true,
      },
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, 'HenryHamster', 'BlueprintAIStudio', WS, STATUS_REF());

    expect(writeDiagnosticsAfterEditHook).toHaveBeenCalledTimes(1);
    const opts = capturedDiagnosticsHeartbeatOpts();
    expect(opts?.diagnostics_after_edit_arm).toBeUndefined();
    expect(opts?.had_diagnostics_after_edit).toBe(true);
  });

  it('writes no hook and sends a false receipt with no arm key when holdbackFraction is 0 and the kill switch is off', async () => {
    const task = {
      title: 'Task',
      description: '',
      context: {},
      project_context: {},
      project_metadata: {
        diagnostics_after_edit_holdback_fraction: 0.0,
        diagnostics_after_edit_enabled: false,
      },
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, 'HenryHamster', 'BlueprintAIStudio', WS, STATUS_REF());

    expect(writeDiagnosticsAfterEditHook).not.toHaveBeenCalled();
    const opts = capturedDiagnosticsHeartbeatOpts();
    expect(opts?.diagnostics_after_edit_arm).toBeUndefined();
    expect(opts?.had_diagnostics_after_edit).toBe(false);
  });

  it('omits diagnostics keys and writes no hook for Codex tasks', async () => {
    vi.mocked(getProviderRunner).mockReturnValue({ displayName: 'OpenAI Codex', provider: 'openai' } as ReturnType<typeof getProviderRunner>);
    const task = {
      title: 'Task',
      description: '',
      context: {},
      project_context: {},
      project_metadata: {
        diagnostics_after_edit_holdback_fraction: 0.5,
        diagnostics_after_edit_enabled: true,
      },
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, 'HenryHamster', 'BlueprintAIStudio', WS, STATUS_REF());

    expect(writeDiagnosticsAfterEditHook).not.toHaveBeenCalled();
    const opts = capturedRepoMapHeartbeatOpts();
    expect(opts?.diagnostics_after_edit_arm).toBeUndefined();
    expect(opts?.had_diagnostics_after_edit).toBeUndefined();
  });

  it('omits diagnostics keys and writes no hook for foreign repos', async () => {
    const task = {
      title: 'Task',
      description: '',
      context: {},
      project_context: {},
      project_metadata: {
        diagnostics_after_edit_holdback_fraction: 0.5,
        diagnostics_after_edit_enabled: true,
      },
    };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, 'other-owner', 'other-repo', WS, STATUS_REF());

    expect(writeDiagnosticsAfterEditHook).not.toHaveBeenCalled();
    const opts = capturedRepoMapHeartbeatOpts();
    expect(opts?.diagnostics_after_edit_arm).toBeUndefined();
    expect(opts?.had_diagnostics_after_edit).toBeUndefined();
  });
});

// ── resolveVisualAcceptance unit tests ────────────────────────────────────────
// This is the linchpin mapping planner output into the success-path capture
// config. A bug here silently makes visual evidence unsatisfiable — the exact
// failure mode this feature exists to fix.
describe('resolveVisualAcceptance', () => {
  // ── payload channel ─────────────────────────────────────────────────────

  it('resolves from payload acceptance_evidence (component + viewport_width)', () => {
    const payload = {
      acceptance_evidence: { kind: 'visual', component: 'Button', viewport_width: 1280 },
    };
    expect(resolveVisualAcceptance(payload, {})).toEqual({ component: 'Button', width: 1280 });
  });

  it('resolves from payload with numeric viewport_width as string', () => {
    const payload = {
      acceptance_evidence: { kind: 'visual', component: 'Card', viewport_width: '768' },
    };
    expect(resolveVisualAcceptance(payload, {})).toEqual({ component: 'Card', width: 768 });
  });

  // ── context channel ──────────────────────────────────────────────────────

  it('resolves from context acceptance_evidence (descriptor + selector)', () => {
    const ctx = {
      acceptance_evidence: { kind: 'visual', descriptor: 'Modal', selector: '1024' },
    };
    expect(resolveVisualAcceptance({}, ctx)).toEqual({ component: 'Modal', width: 1024 });
  });

  it('prefers payload channel over context channel', () => {
    const payload = {
      acceptance_evidence: { kind: 'visual', component: 'Button', viewport_width: 800 },
    };
    const ctx = {
      acceptance_evidence: { kind: 'visual', descriptor: 'OtherComponent', selector: '400' },
    };
    expect(resolveVisualAcceptance(payload, ctx)).toEqual({ component: 'Button', width: 800 });
  });

  // ── SAFE_COMPONENT rejections ────────────────────────────────────────────

  it('returns undefined when component contains a space (not shell-safe)', () => {
    const payload = {
      acceptance_evidence: { kind: 'visual', component: 'My Button', viewport_width: 800 },
    };
    expect(resolveVisualAcceptance(payload, {})).toBeUndefined();
  });

  it('returns undefined when component contains a slash', () => {
    const payload = {
      acceptance_evidence: { kind: 'visual', component: 'a/b', viewport_width: 800 },
    };
    expect(resolveVisualAcceptance(payload, {})).toBeUndefined();
  });

  it('returns undefined when component is empty string', () => {
    const payload = {
      acceptance_evidence: { kind: 'visual', component: '', viewport_width: 800 },
    };
    expect(resolveVisualAcceptance(payload, {})).toBeUndefined();
  });

  it('returns undefined when component exceeds 100 chars', () => {
    const payload = {
      acceptance_evidence: { kind: 'visual', component: 'A'.repeat(101), viewport_width: 800 },
    };
    expect(resolveVisualAcceptance(payload, {})).toBeUndefined();
  });

  it('accepts component at exactly 100 chars', () => {
    const component = 'A'.repeat(100);
    const payload = {
      acceptance_evidence: { kind: 'visual', component, viewport_width: 800 },
    };
    expect(resolveVisualAcceptance(payload, {})).toEqual({ component, width: 800 });
  });

  // ── width 1–4096 boundary ────────────────────────────────────────────────

  it('returns undefined for width 0 (below minimum)', () => {
    const payload = {
      acceptance_evidence: { kind: 'visual', component: 'Btn', viewport_width: 0 },
    };
    expect(resolveVisualAcceptance(payload, {})).toBeUndefined();
  });

  it('returns undefined for width 4097 (above maximum)', () => {
    const payload = {
      acceptance_evidence: { kind: 'visual', component: 'Btn', viewport_width: 4097 },
    };
    expect(resolveVisualAcceptance(payload, {})).toBeUndefined();
  });

  it('accepts width 1 (minimum)', () => {
    const payload = {
      acceptance_evidence: { kind: 'visual', component: 'Btn', viewport_width: 1 },
    };
    expect(resolveVisualAcceptance(payload, {})).toEqual({ component: 'Btn', width: 1 });
  });

  it('accepts width 4096 (maximum)', () => {
    const payload = {
      acceptance_evidence: { kind: 'visual', component: 'Btn', viewport_width: 4096 },
    };
    expect(resolveVisualAcceptance(payload, {})).toEqual({ component: 'Btn', width: 4096 });
  });

  it('returns undefined for non-integer width (float)', () => {
    const payload = {
      acceptance_evidence: { kind: 'visual', component: 'Btn', viewport_width: 800.5 },
    };
    expect(resolveVisualAcceptance(payload, {})).toBeUndefined();
  });

  // ── non-visual kind ──────────────────────────────────────────────────────

  it('returns undefined when payload kind is not visual', () => {
    const payload = {
      acceptance_evidence: { kind: 'functional', component: 'Btn', viewport_width: 800 },
    };
    expect(resolveVisualAcceptance(payload, {})).toBeUndefined();
  });

  it('returns undefined when context kind is not visual', () => {
    const ctx = {
      acceptance_evidence: { kind: 'named_test', descriptor: 'Btn', selector: '800' },
    };
    expect(resolveVisualAcceptance({}, ctx)).toBeUndefined();
  });

  // ── missing / malformed ──────────────────────────────────────────────────

  it('returns undefined when no acceptance_evidence present', () => {
    expect(resolveVisualAcceptance({}, {})).toBeUndefined();
  });

  it('returns undefined when acceptance_evidence is null in payload', () => {
    const payload = { acceptance_evidence: null };
    expect(resolveVisualAcceptance(payload, {})).toBeUndefined();
  });

  it('returns undefined when acceptance_evidence is a string (not an object)', () => {
    const payload = { acceptance_evidence: 'visual' };
    expect(resolveVisualAcceptance(payload, {})).toBeUndefined();
  });
});

describe('prompt size telemetry', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetLlmTelemetry();
  });

  it('records prompt size telemetry in the standard handler', async () => {
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    await handleCreate(TASK_ID, { prompt: 'Do something' }, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const telemetry = getLlmTelemetry();

    // prompt_size_bytes must be the real assembled-prompt byte length — positive
    expect(telemetry.prompt_size_bytes).toBeGreaterThan(0);
    expect(telemetry.prompt_section_count).toBeGreaterThan(0);
    expect(telemetry.prompt_section_sizes).not.toBeNull();
    // 'prompt_body' must NOT appear as a section (the whole prompt is not a component of itself)
    expect((telemetry.prompt_section_sizes ?? []).map(s => s.name)).not.toContain('prompt_body');
    // Component sections summed must not exceed the total (no double-counting)
    const sectionSum = (telemetry.prompt_section_sizes ?? []).reduce((s, e) => s + e.bytes, 0);
    expect(sectionSum).toBeLessThanOrEqual(telemetry.prompt_size_bytes ?? 0);
    // Top 3 sections should have named entries with positive byte counts
    expect((telemetry.prompt_section_sizes ?? []).length).toBeGreaterThan(0);
    for (const s of (telemetry.prompt_section_sizes ?? [])) {
      expect(s.bytes).toBeGreaterThan(0);
      expect(typeof s.name).toBe('string');
    }
  });

  it('buildPromptSizeTelemetry sorts sections descending and caps at 10', () => {
    // 12 sections — only the top 10 by size should survive the cap
    const sections: Array<[string, string]> = [
      ['s1', 'x'.repeat(200)],
      ['s2', 'x'.repeat(180)],
      ['s3', 'x'.repeat(160)],
      ['s4', 'x'.repeat(140)],
      ['s5', 'x'.repeat(120)],
      ['s6', 'x'.repeat(100)],
      ['s7', 'x'.repeat(80)],
      ['s8', 'x'.repeat(60)],
      ['s9', 'x'.repeat(40)],
      ['s10', 'x'.repeat(20)],
      ['s11', 'x'.repeat(10)],  // smallest, should be dropped
      ['s12', 'x'.repeat(5)],   // smallest, should be dropped
    ];
    const prompt = sections.map(([, text]) => text).join('\n');
    const result = buildPromptSizeTelemetry(prompt, sections);
    expect(result.prompt_size_bytes).toBe(Buffer.byteLength(prompt, 'utf8'));
    expect(result.prompt_section_count).toBe(12);
    expect(result.prompt_section_sizes).toHaveLength(10);
    // Sorted descending: s1 (largest) .. s10
    expect(result.prompt_section_sizes[0].name).toBe('s1');
    expect(result.prompt_section_sizes[9].name).toBe('s10');
    // Every entry has bytes >= the next
    for (let i = 0; i < result.prompt_section_sizes.length - 1; i++) {
      expect(result.prompt_section_sizes[i].bytes)
        .toBeGreaterThanOrEqual(result.prompt_section_sizes[i + 1].bytes);
    }
    // The dropped sections (s11, s12) must NOT be in the result
    const names = result.prompt_section_sizes.map((s: { name: string; bytes: number }) => s.name);
    expect(names).not.toContain('s11');
    expect(names).not.toContain('s12');
  });

  it('setPromptSizeTelemetry persists sections into getLlmTelemetry', () => {
    resetLlmTelemetry();
    setPromptSizeTelemetry(500, 3, [
      { name: 'core', bytes: 200 },
      { name: 'claude_md', bytes: 150 },
      { name: 'guidance', bytes: 100 },
    ]);
    const telemetry = getLlmTelemetry();
    expect(telemetry.prompt_size_bytes).toBe(500);
    expect(telemetry.prompt_section_count).toBe(3);
    expect(telemetry.prompt_section_sizes).toEqual([
      { name: 'core', bytes: 200 },
      { name: 'claude_md', bytes: 150 },
      { name: 'guidance', bytes: 100 },
    ]);
  });

  it('runCodeChangeTask does not set telemetry — callers set richer per-section decompositions', async () => {
    // runCodeChangeTask no longer calls setPromptSizeTelemetry. The comment at
    // code-change.ts:9-11 explains that callers (standard.ts, review-fix.ts,
    // specialized.ts) set richer per-section decompositions before invoking
    // runCodeChangeTask.  This test confirms runCodeChangeTask does NOT
    // overwrite caller-set telemetry.
    const real = await vi.importActual<typeof import('./code-change.js')>('./code-change.js');
    const realRunCodeChangeTask = real.runCodeChangeTask;
    // This telemetry-only test owns no workspace diff. Keep HEAD equal to the
    // supplied base so it cannot enter the emergency checkpoint/push path.
    vi.mocked(git).mockImplementation((args: string[]) =>
      args[0] === 'rev-parse' ? 'abc123' : '');

    await realRunCodeChangeTask({
      taskId: 'test-telemetry-cc',
      prompt: 'Test code-change prompt for telemetry verification',
      ws: null,
      statusRef: { value: '' },
      workBranch: 'task-cc-telemetry',
      baseBranch: 'main',
      owner: 'owner',
      repo: 'repo',
      testsOnMainBranch: false,
      baseCommit: 'abc123',
    });

    // Telemetry should remain in its reset state — runCodeChangeTask
    // no longer owns prompt-size reporting; callers do.
    const telemetry = getLlmTelemetry();
    expect(telemetry.prompt_size_bytes).toBeNull();
    expect(telemetry.prompt_section_count).toBeNull();
    expect(telemetry.prompt_section_sizes).toBeNull();
  });
});

// ── Salvage bundle handler ──────────────────────────────────────────────
describe('handleSalvageBundle', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('includes finding prompt content in the built prompt string', async () => {
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    const payload = {
      salvaged_findings: [
        {
          title: 'Fix flaky test',
          discard_reason: 'decayed',
          prompt: 'Investigate the flaky websocket reconnect test — add proper async wait logic and ensure the test does not race against the server heartbeat.',
          description: 'short description',
          related_files: ['tests/test_ws.py'],
        },
      ],
    };
    await handleSalvageBundle(TASK_ID, payload, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).toContain('Fix flaky test');
    expect(prompt).toContain('Investigate the flaky websocket reconnect test');
    expect(prompt).toContain('tests/test_ws.py');
  });

  it('falls back to description when prompt is empty', async () => {
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    const payload = {
      salvaged_findings: [
        {
          title: 'Refactor helper',
          discard_reason: 'decayed',
          prompt: '',
          description: 'This is a long description that should appear in the prompt when prompt is empty.',
          related_files: ['src/helper.ts'],
        },
      ],
    };
    await handleSalvageBundle(TASK_ID, payload, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).toContain('Refactor helper');
    expect(prompt).toContain('This is a long description that should appear');
    expect(prompt).not.toContain('Prompt:');
  });

  it('includes claims as a bulleted list when present', async () => {
    const task = { title: 'Task', description: '', context: {}, project_context: {} };
    const payload = {
      salvaged_findings: [
        {
          title: 'Memory leak in cache',
          discard_reason: 'decayed',
          prompt: 'Fix the unbounded cache growth in the metrics aggregator.',
          claims: [
            'Cache entries are never evicted after TTL expiry',
            'Aggregator retains references to all metric keys',
            'Heap grows linearly with unique metric count over uptime',
          ],
          related_files: ['src/cache.ts'],
        },
      ],
    };
    await handleSalvageBundle(TASK_ID, payload, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedPrompt();
    expect(prompt).toContain('Cache entries are never evicted after TTL expiry');
    expect(prompt).toContain('Aggregator retains references to all metric keys');
    expect(prompt).toContain('Heap grows linearly');
  });
});
