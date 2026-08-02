import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── Type-safe mock export names ────────────────────────────────────────────────
// Ensures that renaming/removing an export in the mocked module causes tsc to
// fail at compile time. Each type assertion verifies the array only contains
// valid export keys from the actual module.
const {
  INDEX_MOCK_EXPORTS,
  WORKSPACE_PREP_MOCK_EXPORTS,
  PROMPT_MOCK_EXPORTS,
  BUILD_CONFIG_MOCK_EXPORTS,
  TELEMETRY_MOCK_EXPORTS,
  CODE_CHANGE_MOCK_EXPORTS,
} = vi.hoisted(() => {
  type IndexExportKey = keyof typeof import('../index.js');
  type WorkspacePrepExportKey = keyof typeof import('../workspacePrep.js');
  type PromptExportKey = keyof typeof import('../prompt.js');
  type BuildConfigExportKey = keyof typeof import('../build-config.js');
  type TelemetryExportKey = keyof typeof import('../telemetry.js');
  type CodeChangeExportKey = keyof typeof import('./code-change.js');

  return {
    INDEX_MOCK_EXPORTS: [
      'WORKSPACE', 'sendLog', 'log', 'getAgentToken', 'apiPost', 'apiGet',
      'persistAndPostStatus', 'readHandlerOutput', 'validateBranchName', 'git',
      'gitAsync', 'setupWorkspace', 'runClaudeCode', 'cloneWithRetry',
      'sendHeartbeatSync', 'listOpenPullRequestsForBranch',
    ] satisfies readonly IndexExportKey[],
    WORKSPACE_PREP_MOCK_EXPORTS: [
      'prepareWorkspaceForRole',
    ] satisfies readonly WorkspacePrepExportKey[],
    PROMPT_MOCK_EXPORTS: [
      'getTaskTypeHints', 'readClaudeMd', 'buildContextPrefix', 'buildFileTree',
      'discoverRelatedTests', 'configureAgentPermissions', 'writePlaybookHelper',
      'writeMessagesHelper', 'writeCodeChangeHelpers', 'formatPlaybook', 'buildRetryContext',
    ] satisfies readonly PromptExportKey[],
    BUILD_CONFIG_MOCK_EXPORTS: [
      'resolveBuildConfig',
    ] satisfies readonly BuildConfigExportKey[],
    TELEMETRY_MOCK_EXPORTS: [
      'recordFormatMiss', 'recordFormatRepairAttempt', 'recordFormatRepairSuccess',
    ] satisfies readonly TelemetryExportKey[],
    CODE_CHANGE_MOCK_EXPORTS: [
      'runCodeChangeTask',
    ] satisfies readonly CodeChangeExportKey[],
  };
});

// Used for type checking only; suppress "unused" warnings
void INDEX_MOCK_EXPORTS; void WORKSPACE_PREP_MOCK_EXPORTS; void PROMPT_MOCK_EXPORTS; void BUILD_CONFIG_MOCK_EXPORTS; void TELEMETRY_MOCK_EXPORTS; void CODE_CHANGE_MOCK_EXPORTS;

// ── Module mocks ──────────────────────────────────────────────────────────────
// 2026-05-04: handleMigrate no longer drives commit/rebase/push/PR via the
// wrapper. apiPost('/api/agent/github/pull-requests', ...) and the
// commit_hash/repo_url/pr_url status fields are gone — Claude calls
// ./pr open + ./done from inside its own session via runCodeChangeTask
// (agent/src/handlers/code-change.ts). Tests therefore mock runCodeChangeTask
// (default no-op) and assert on its arguments — primarily the `prompt` and
// the rest of the opts wiring (workBranch, baseBranch, owner, repo,
// disallowedTools, noChangesMessage). Stale-branch handling (ls-remote +
// listOpenPullRequestsForBranch) still runs PRE-Claude and remains tested.
//
// The other handlers in this file (handleSecurityAudit, handleSmoketest,
// handleDebug, handlePerf, handleInvestigate, handleSelfAudit) still drive
// their own runClaudeCode invocation directly — those tests are unchanged.
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
    persistAndPostStatus: vi.fn().mockResolvedValue(undefined),
    readHandlerOutput: vi.fn(),
    validateBranchName: vi.fn((b: string, _label: string) => b),
    git: vi.fn().mockReturnValue(''),
    gitAsync: vi.fn().mockResolvedValue(''),
    setupWorkspace: vi.fn(),
    runClaudeCode: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' }),
    cloneWithRetry: vi.fn().mockResolvedValue(undefined),
    sendHeartbeatSync: vi.fn(),
    listOpenPullRequestsForBranch: vi.fn().mockResolvedValue([]),
  };
});

vi.mock('../workspacePrep.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../workspacePrep.js')>();
  return { ...actual, prepareWorkspaceForRole: vi.fn().mockResolvedValue(undefined) };
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
  };
});

vi.mock('../build-config.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../build-config.js')>();
  return { ...actual, resolveBuildConfig: vi.fn().mockReturnValue(undefined) };
});

vi.mock('../telemetry.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../telemetry.js')>();
  return {
    ...actual,
    recordFormatMiss: vi.fn(),
    recordFormatRepairAttempt: vi.fn(),
    recordFormatRepairSuccess: vi.fn(),
  };
});

// Mock the code-change module so handleMigrate tests can assert on what was
// passed to runCodeChangeTask without exercising the marker-detection branches.
vi.mock('./code-change.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./code-change.js')>();
  return { ...actual, runCodeChangeTask: vi.fn().mockResolvedValue(undefined) };
});

// fs is consumed by both default + namespace imports across the modules under
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
// Import handlers/index.ts first so audits.ts is loaded in the test module
// context before the importOriginal chain (inside vi.mock('../index.js', ...))
// can load it from a different path.  Without this, the direct import of
// audits.ts in handlers/index.ts creates a shorter circular-module path that
// causes audits.ts to bind to the actual ../index.ts instead of the mock.
import './index.js';
import {
  handleSecurityAudit,
  handleSmoketest,
  handleDebug,
  handlePerf,
  handleInvestigate,
  handleRepoBootstrap,
  handleMigrate,
  handleSelfAudit,
  resolveMigrationLayout,
  isRepoBootstrapOverview,
  isCommandMemoryOutcome,
  assembleRepoBootstrapPrompt,
} from './specialized.js';
import {
  persistAndPostStatus, readHandlerOutput, runClaudeCode,
  git, listOpenPullRequestsForBranch, sendLog,
} from '../index.js';
import { resolveBuildConfig } from '../build-config.js';
import { runCodeChangeTask } from './code-change.js';
import { execFileSync } from 'child_process';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import {
  PARALLEL_AUDIT_GUIDANCE, READ_ONLY_BASH_WRITE_NOTE, DEFAULT_TOOL_GUIDANCE,
  PLAYBOOK_GUIDANCE, BUILD_WITH_INTENT_GUIDANCE, BUILD_ORDER_GUIDANCE,
  buildContextPrefix,
} from '../prompt.js';
import { recordFormatMiss, recordFormatRepairAttempt, recordFormatRepairSuccess } from '../telemetry.js';
import { expectCalledWith } from '../utils/typed-mock-call.js';

// ── Shared test helpers ───────────────────────────────────────────────────────
const TASK_ID = 'task-abc123';
const GH_TOKEN = 'gh-token';
const OWNER = 'owner';
const REPO = 'repo';
const WS = null;
const STATUS_REF = () => ({ value: '' });
const BASE_TASK = { title: 'Test Task', description: '', context: {}, project_context: {} };
const BASE_PAYLOAD = { base_branch: 'main' };
// handleInvestigate hard-fails when no prompt is supplied; provide a default
// for cases where the test isn't exercising that error path.
const INVESTIGATE_PAYLOAD = { ...BASE_PAYLOAD, prompt: 'investigate the issue' };

/** Returns the prompt passed to the latest runCodeChangeTask invocation. */
function capturedMigratePrompt(): string {
  const calls = vi.mocked(runCodeChangeTask).mock.calls;
  if (calls.length === 0) throw new Error('runCodeChangeTask was not invoked');
  return calls[calls.length - 1][0].prompt;
}

/** Returns the full opts object passed to the latest runCodeChangeTask invocation. */
function capturedMigrateOpts(): Parameters<typeof runCodeChangeTask>[0] {
  const calls = vi.mocked(runCodeChangeTask).mock.calls;
  if (calls.length === 0) throw new Error('runCodeChangeTask was not invoked');
  return calls[calls.length - 1][0];
}

// ── handlePerf — JSON extraction fallback ─────────────────────────────────────
describe('handlePerf — JSON extraction fallback', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });

  it('uses readHandlerOutput result directly when it returns findings', async () => {
    const findings = [{ area: 'database', severity: 'high', title: 'N+1 Query' }];
    vi.mocked(readHandlerOutput).mockReturnValue(findings);

    await handlePerf(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.logs).toBe(JSON.stringify(findings));
    expect(call.artifact_type).toBe('PERF_REPORT');
  });

  it('falls back to lastResultText when it contains "severity" and readHandlerOutput returns null', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    const lastResultText = '[{"area":"api","severity":"medium","title":"Unbounded list"}]';
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText });

    await handlePerf(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.logs).toBe(lastResultText);
  });

  it('extracts JSON from allOutput via regex when lastResultText lacks "severity"', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    const jsonBlock = '[{"area":"frontend","severity":"low","title":"Missing useMemo"}]';
    const allOutput = `Analysis complete\n${jsonBlock}\nEnd`;
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput, lastResultText: 'no keywords' });

    await handlePerf(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.logs).toBe(jsonBlock);
  });

  it('repairs clean-exit unparseable output once and stores the repaired findings array', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    const repaired = [{ area: 'api', severity: 'medium', title: 'Unbounded list' }];
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'plain text', lastResultText: 'no json' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: JSON.stringify(repaired) });

    await handlePerf(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('PERF_REPORT');
    expect(call.logs).toBe(JSON.stringify(repaired));
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    const reopenPrompt = vi.mocked(runClaudeCode).mock.calls[1][0];
    expect(reopenPrompt).toContain('Output ONLY the JSON array now');
    expect(reopenPrompt).not.toContain('/tmp/');
    expect(reopenPrompt).not.toContain(READ_ONLY_BASH_WRITE_NOTE);
    expect(recordFormatRepairAttempt).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairSuccess).toHaveBeenCalledTimes(1);
  });

  it('sets status COMPLETED and artifact_type PERF_REPORT on exitCode 0', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue([]);

    await handlePerf(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('PERF_REPORT');
  });

  it('sets status FAILED and artifact_type ERROR_LOG on non-zero exitCode', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: 'crash', lastResultText: '' });

    await handlePerf(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
  });
});

// ── handleSmoketest — URL health check result formatting ──────────────────────
describe('handleSmoketest — URL health checks', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });

  it('reports COMPLETED with SMOKETEST_RESULT when smoketestResult.passed is true and no URLs', async () => {
    const smoketestResult = { passed: true, tests: [{ name: 'startup', status: 'pass', message: '' }] };
    vi.mocked(readHandlerOutput).mockReturnValue(smoketestResult);

    await handleSmoketest(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('SMOKETEST_RESULT');
  });

  it('folds candidate env-preflight evidence into SMOKETEST_RESULT on success', async () => {
    const smoketestResult = { passed: true, tests: [{ name: 'startup', status: 'pass', message: '' }] };
    vi.mocked(readHandlerOutput).mockReturnValue(smoketestResult);
    const preflight = {
      required_env: [{ name: 'GITHUB_PAT_ENCRYPTION_KEY', scope: 'cloud_run' }],
      startup_check_names: ['python3 -c "import app.main"'],
      manifest_source: 'backend/app/required_runtime_env.py',
    };
    vi.mocked(execFileSync).mockImplementation((cmd: unknown, args: unknown) => {
      const argsArr = args as string[];
      if (cmd === 'python3' && Array.isArray(argsArr) && argsArr.includes('app.env_preflight_evidence')) {
        return JSON.stringify(preflight);
      }
      return Buffer.from('');
    });

    await handleSmoketest(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    const stored = JSON.parse(call.logs as string) as { passed: boolean; env_preflight?: unknown };
    expect(stored.env_preflight).toEqual(preflight);
    expect(stored.passed).toBe(true);
  });

  it('does not change passed and still persists SMOKETEST_RESULT when env-preflight subprocess fails', async () => {
    const smoketestResult = { passed: true, tests: [{ name: 'startup', status: 'pass', message: '' }] };
    vi.mocked(readHandlerOutput).mockReturnValue(smoketestResult);
    vi.mocked(execFileSync).mockImplementation((cmd: unknown, args: unknown) => {
      const argsArr = args as string[];
      if (cmd === 'python3' && Array.isArray(argsArr) && argsArr.includes('app.env_preflight_evidence')) {
        throw new Error('module import failed');
      }
      return Buffer.from('');
    });

    await handleSmoketest(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    const stored = JSON.parse(call.logs as string) as { passed: boolean; env_preflight?: unknown; env_preflight_error?: string };
    expect(stored.passed).toBe(true);
    expect(stored.env_preflight).toBeUndefined();
    expect(stored.env_preflight_error).toBeTruthy();
  });

  it('reports FAILED with SMOKETEST_RESULT for an ordinary genuine test failure', async () => {
    const smoketestResult = { passed: false, tests: [{ name: 'pytest', status: 'fail', message: 'AssertionError' }] };
    vi.mocked(readHandlerOutput).mockReturnValue(smoketestResult);

    await handleSmoketest(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('SMOKETEST_RESULT');
    expect(call.failure_category).toBeUndefined();
    const stored = JSON.parse(call.logs as string) as { passed: boolean };
    expect(stored.passed).toBe(false);
  });

  it('instructs setup and execution blockers to use error rather than skip', async () => {
    const smoketestResult = { passed: false, tests: [{ name: 'setup', status: 'error', message: 'database unavailable' }] };
    vi.mocked(readHandlerOutput).mockReturnValue(smoketestResult);

    await handleSmoketest(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('Use status "error" for collection, setup, environment, or command-execution failures');
    expect(prompt).toContain('Use status "skip" only when a test is intentionally inapplicable');
  });


  it('appends passing URL health check results and keeps passed=true', async () => {
    const smoketestResult = { passed: true, tests: [] as Array<{ name: string; status: string; message: string }> };
    vi.mocked(readHandlerOutput).mockReturnValue(smoketestResult);
    // curl succeeds (no throw)
    vi.mocked(execFileSync).mockReturnValue(Buffer.from(''));

    await handleSmoketest(
      TASK_ID,
      { ...BASE_PAYLOAD, health_check_urls: ['https://example.com/health'] },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    const stored = JSON.parse(call.logs as string) as { passed: boolean; tests: Array<{ name: string; status: string }> };
    expect(stored.passed).toBe(true);
    expect(stored.tests).toHaveLength(1);
    expect(stored.tests[0].name).toBe('HTTP GET https://example.com/health');
    expect(stored.tests[0].status).toBe('pass');
  });

  it('does not let a passing URL health check erase an earlier test failure', async () => {
    const smoketestResult = { passed: false, tests: [{ name: 'pytest', status: 'fail', message: 'AssertionError' }] };
    vi.mocked(readHandlerOutput).mockReturnValue(smoketestResult);
    vi.mocked(execFileSync).mockReturnValue(Buffer.from(''));

    await handleSmoketest(
      TASK_ID,
      { ...BASE_PAYLOAD, health_check_urls: ['https://example.com/health'] },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    const stored = JSON.parse(call.logs as string) as { passed: boolean; tests: Array<{ status: string }> };
    expect(stored.passed).toBe(false);
    expect(stored.tests).toHaveLength(2);
    expect(stored.tests[1].status).toBe('pass');
  });

  it('appends failing URL health check results and flips passed to false', async () => {
    const smoketestResult = { passed: true, tests: [] as Array<{ name: string; status: string; message: string }> };
    vi.mocked(readHandlerOutput).mockReturnValue(smoketestResult);
    // curl fails for this specific call
    vi.mocked(execFileSync).mockImplementation((cmd: unknown, args: unknown) => {
      const argsArr = args as string[];
      if (cmd === 'curl' || (Array.isArray(argsArr) && argsArr.includes('https://example.com/down'))) {
        const err = new Error('curl failed') as Error & { status: number };
        err.status = 22;
        throw err;
      }
      return Buffer.from('');
    });

    await handleSmoketest(
      TASK_ID,
      { ...BASE_PAYLOAD, health_check_urls: ['https://example.com/down'] },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    const stored = JSON.parse(call.logs as string) as { passed: boolean; tests: Array<{ name: string; status: string; message: string }> };
    expect(stored.passed).toBe(false);
    expect(stored.tests[0].status).toBe('fail');
    expect(stored.tests[0].message).toContain('22');
  });

  it('falls back to ERROR_LOG when readHandlerOutput returns null or exitCode is non-zero', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: 'fail output', lastResultText: '' });

    await handleSmoketest(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
  });

  it('sets failure_category to smoketest_result_unparseable when exitCode=0 but result file is missing', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: 'output but no result file', lastResultText: '' });

    await handleSmoketest(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
    expect(call.failure_category).toBe('smoketest_result_unparseable');
  });

  it('rejects a non-boolean top-level passed value as unparseable', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue({
      passed: 'false',
      tests: [{ name: 'health', status: 'pass', message: '' }],
    });
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: 'wrote malformed result', lastResultText: '' });

    await handleSmoketest(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
    expect(call.failure_category).toBe('smoketest_result_unparseable');
  });

  it('sets failure_category to claude_code_failed when exitCode≠0 and result file is missing', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: 'agent crashed', lastResultText: '' });

    await handleSmoketest(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
    expect(call.failure_category).toBe('claude_code_failed');
  });
});

// ── handleDebug — report file reading and status mapping ─────────────────────
describe('handleDebug — report reading and status mapping', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });

  it('sets status COMPLETED and artifact_type DEBUG_RESULT when debugResult is found', async () => {
    const debugResult = {
      root_cause: 'Missing null check at line 42',
      affected_files: ['src/utils.ts'],
      fix_prompt: 'Add null check',
      confidence: 'high',
    };
    vi.mocked(readHandlerOutput).mockReturnValue(debugResult);

    await handleDebug(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('DEBUG_RESULT');
    expect(call.logs).toBe(JSON.stringify(debugResult));
  });

  it('sets status FAILED and artifact_type ERROR_LOG when debugResult is null', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: 'no report written', lastResultText: '' });

    await handleDebug(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
  });
});

// ── handleInvestigate — report file reading and status mapping ────────────────
describe('handleInvestigate — report reading and status mapping', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });

  it('sets status COMPLETED and artifact_type INVESTIGATION_REPORT on exitCode 0 with result', async () => {
    const investigationResult = {
      summary: 'Found issue in auth middleware',
      root_cause: 'Token not invalidated on logout',
      affected_files: [{ path: 'backend/app/auth.py', issue: 'missing invalidation' }],
      recommended_fix: 'Call invalidate() in logout handler',
      severity: 'high',
      confidence: 'high',
    };
    vi.mocked(readHandlerOutput).mockReturnValue(investigationResult);

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('INVESTIGATION_REPORT');
    expect(call.logs).toBe(JSON.stringify(investigationResult));
  });

  it('sets status FAILED with parse_failed category when investigation_report file is missing', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('parse_failed');
  });

  it('sanitizes logs field when investigation report cannot be parsed (no GitHub token leak)', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    // repairStructuredOutput also returns null: mock two runClaudeCode calls
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: 'cloning https://x-access-token:ghp_SECRETTOKEN@github.com/owner/repo.git',
        lastResultText: 'no json here',
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: 'still no json',
      });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('parse_failed');
    // sanitize() replaces the token value but keeps the scheme prefix — verify
    // the raw credential is gone, not the entire URL pattern
    expect(call.logs).not.toContain('ghp_SECRETTOKEN');
  });

  it('recovers investigation JSON from lastResultText when file is missing', async () => {
    const recovered = {
      summary: 'Auth token not invalidated',
      root_cause: 'Missing invalidation call in logout handler',
      affected_files: [{ path: 'backend/app/auth.py', issue: 'no invalidation' }],
      recommended_fix: 'Call invalidate() in logout()',
      severity: 'high',
      confidence: 'medium',
    };
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: JSON.stringify(recovered),
    });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('INVESTIGATION_REPORT');
    expect(call.logs).toBe(JSON.stringify(recovered));
  });

  it('recovers investigation JSON from allOutput when file and lastResultText are both missing', async () => {
    const recovered = {
      summary: 'Null pointer in parser',
      root_cause: 'Parser assumes non-null input',
      affected_files: [{ path: 'src/parser.ts', issue: 'no null guard' }],
      recommended_fix: 'Add null check before parsing',
      severity: 'medium',
      confidence: 'high',
    };
    const prose = `I investigated the issue.\n\nHere is my report:\n${JSON.stringify(recovered)}\n\nHope this helps.`;
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: prose,
      lastResultText: '',
    });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('INVESTIGATION_REPORT');
    expect(call.logs).toBe(JSON.stringify(recovered));
  });

  it('falls through to parse_failed when lastResultText has malformed JSON with required keys', async () => {
    // JsonExtractionError is thrown when the transcript contains a balanced JSON
    // object with the required keys but the content fails JSON.parse (e.g. JS object
    // literal with unquoted values). The try/catch in handleInvestigate must catch
    // this and treat it as "not found" rather than letting it propagate as a fatal error.
    const malformedJson = '{"root_cause": undefined, "recommended_fix": "fix it"}';
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: malformedJson,
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: malformedJson,
      });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('parse_failed');
    expect(recordFormatRepairAttempt).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairSuccess).not.toHaveBeenCalled();
  });

  it('falls through to parse_failed when allOutput has malformed JSON with required keys', async () => {
    const malformedJson = 'Investigation complete.\n{"root_cause": undefined, "recommended_fix": "fix it"}\nDone.';
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: malformedJson,
        lastResultText: '',
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: malformedJson,
        lastResultText: '',
      });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('parse_failed');
  });

  it('recovers clean-exit unparseable investigation output via bounded re-ask instead of failing', async () => {
    const recovered = {
      summary: 'Auth token not invalidated',
      root_cause: 'Missing invalidation call in logout handler',
      affected_files: [{ path: 'backend/app/auth.py', issue: 'no invalidation' }],
      recommended_fix: 'Call invalidate() in logout()',
      severity: 'high',
      confidence: 'medium',
    };
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: 'investigation complete but prose only',
        lastResultText: 'no json object here',
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify(recovered),
      });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('INVESTIGATION_REPORT');
    expect(call.failure_category).toBeUndefined();
    expect(call.logs).toBe(JSON.stringify(recovered));
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(runClaudeCode).mock.calls[1][0]).toContain('Output ONLY the JSON object now');
    expect(vi.mocked(runClaudeCode).mock.calls[1][0]).not.toContain('/tmp/');
    expect(recordFormatRepairAttempt).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairSuccess).toHaveBeenCalledTimes(1);
  });

  it('investigate prompt includes inline output fallback', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    const priorOutput = 'my prior investigation text';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: priorOutput,
        lastResultText: '',
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          summary: 's', root_cause: 'r', affected_files: [], recommended_fix: 'fix', severity: 'low', confidence: 'high',
        }),
      });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // Initial prompt must include the inline output fallback instruction
    const initialPrompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(initialPrompt).toContain('Also output the same JSON object as the final output in your logs');

    // Reopen prompt must include the prior output as context
    const reopenPrompt = vi.mocked(runClaudeCode).mock.calls[1][0];
    expect(reopenPrompt).toContain(priorOutput);
  });

  it('sets status FAILED and artifact_type ERROR_LOG on non-zero exitCode with result', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue({ summary: 'partial', root_cause: '', affected_files: [], recommended_fix: '', severity: 'low', confidence: 'low' });
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: '', lastResultText: '' });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
  });

  it('posts rate_limited instead of parse_failed when transcript contains a weekly-limit message', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    const rateLimitTranscript = "You've hit your weekly limit · resets 9pm (UTC)\nNo output produced.";
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: rateLimitTranscript, lastResultText: '' });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('rate_limited');
  });

  it('posts rate_limited instead of parse_failed when lastResultText contains a weekly-limit message', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 1,
      allOutput: '',
      lastResultText: "You've hit your weekly limit · resets 9pm (UTC)",
    });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.failure_category).toBe('rate_limited');
  });

  it('throws when no prompt is provided in payload or task', async () => {
    await expect(
      handleInvestigate(
        TASK_ID,
        BASE_PAYLOAD,
        { title: '', description: '', context: {}, project_context: {} },
        GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
      ),
    ).rejects.toThrow(/No prompt found/);
  });
});

// ── handleInvestigate — adversarial spawn-gate grader branch ──────────────────
describe('handleInvestigate — adversarial grader', () => {
  // The adversarial challenge prompt already carries its own output contract:
  // a JSON array of [{hash, verdict, reason}] objects the backend parser anchors
  // on. The handler must bypass the investigation template entirely and emit the
  // raw challenger output as logs.
  const ADVERSARIAL_TASK = {
    ...BASE_TASK,
    context: { adversarial_grader: true, _adversarial_challenged_hashes: ['abc123'] },
  };
  const VERDICT_ARRAY = '[{"hash": "abc123", "verdict": "fail", "reason": "premise no longer holds at HEAD"}]';

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });

  it('runs the raw payload prompt and persists the verdict array verbatim as logs', async () => {
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: VERDICT_ARRAY });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, ADVERSARIAL_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // The prompt passed to Claude is the raw payload prompt (no investigation
    // template wrapping) — it must NOT instruct writing the summary/root_cause
    // schema to /tmp/investigation_report_*.json.
    const runPrompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(runPrompt).toContain('investigate the issue');
    expect(runPrompt).not.toContain('You are an investigation agent');
    expect(runPrompt).not.toContain('investigation_report_');

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('INVESTIGATION_REPORT');
    // logs carry the raw [{hash, verdict, reason}] array, not a JSON.stringify of
    // a summary/root_cause investigation object.
    expect(call.logs).toContain('"hash"');
    expect(call.logs).toContain('"verdict"');
    expect(call.logs).toBe(VERDICT_ARRAY);
  });

  it('does NOT consult readHandlerOutput on the adversarial branch', async () => {
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: VERDICT_ARRAY });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, ADVERSARIAL_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(readHandlerOutput)).not.toHaveBeenCalled();
  });

  it('falls back to the transcript tail when no final text is surfaced', async () => {
    const prose = `Challenged each finding.\n${VERDICT_ARRAY}`;
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: prose, lastResultText: '' });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, ADVERSARIAL_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.logs).toBe(prose);
  });

  it('sets status FAILED with claude_code_failed on non-zero exitCode', async () => {
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: 'crashed', lastResultText: '' });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, ADVERSARIAL_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
    expect(call.failure_category).toBe('claude_code_failed');
  });

  it('regression guard: a non-adversarial task still uses the investigation template', async () => {
    const investigationResult = {
      summary: 'Found issue', root_cause: 'cause', affected_files: [],
      recommended_fix: 'fix', severity: 'low', confidence: 'high',
    };
    vi.mocked(readHandlerOutput).mockReturnValue(investigationResult);

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const runPrompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(runPrompt).toContain('You are an investigation agent');
    expect(vi.mocked(readHandlerOutput)).toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.artifact_type).toBe('INVESTIGATION_REPORT');
    expect(call.logs).toBe(JSON.stringify(investigationResult));
  });
});

// ── handleInvestigate — adversarial grader self-repair ────────────────────────
describe('handleInvestigate — adversarial grader self-repair', () => {
  const ADVERSARIAL_TASK_WITH_HASHES = {
    ...BASE_TASK,
    context: { adversarial_grader: true, _adversarial_challenged_hashes: ['abc123', 'def456'] },
  };
  const VERDICT_ARRAY = '[{"hash":"abc123","verdict":"pass","reason":"still holds at HEAD"},{"hash":"def456","verdict":"fail","reason":"premise no longer present"}]';
  const VERDICT_ARRAY_PARSED: unknown[] = JSON.parse(VERDICT_ARRAY) as unknown[];

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('repairs a malformed challenger response into a [{hash, verdict, reason}] array via focused re-ask', async () => {
    // First call: clean exit but only prose — no JSON array.
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: 'I reviewed the findings. abc123 still holds, def456 is not present anymore.', lastResultText: '' });
    // Second call (repair re-ask): valid verdict array in lastResultText.
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: VERDICT_ARRAY });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, ADVERSARIAL_TASK_WITH_HASHES, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // Repair should have triggered exactly one re-ask.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(recordFormatRepairAttempt)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairSuccess)).toHaveBeenCalledTimes(1);

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('INVESTIGATION_REPORT');
    // logs carry the repaired [{hash, verdict, reason}] array.
    expect(JSON.parse(call.logs as string)).toEqual(VERDICT_ARRAY_PARSED);
  });

  it('includes the challenged hashes from context in the re-ask prompt', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: 'no JSON here', lastResultText: '' });
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: VERDICT_ARRAY });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, ADVERSARIAL_TASK_WITH_HASHES, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // The repair re-ask (second call) must mention each challenged hash so the
    // model can re-emit a complete array.
    const reaskPrompt = vi.mocked(runClaudeCode).mock.calls[1][0];
    expect(reaskPrompt).toContain('abc123');
    expect(reaskPrompt).toContain('def456');
  });

  it('posts COMPLETED with raw output when repair also fails to produce a parseable array', async () => {
    const prose = 'abc123 is still valid, def456 is not present.';
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: prose, lastResultText: '' });
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: 'Still no JSON array.', lastResultText: '' });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, ADVERSARIAL_TASK_WITH_HASHES, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    // Raw output preserved — backend parser will fail-close findings gracefully.
    expect(call.status).toBe('COMPLETED');
    expect(call.logs).toContain(prose);
  });

  it('skips repair and posts FAILED when exitCode is non-zero', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 1, allOutput: 'crashed', lastResultText: '' });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, ADVERSARIAL_TASK_WITH_HASHES, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // A CLI crash should not trigger a repair re-ask.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('claude_code_failed');
  });

  it('records research_spawn_adversarial_challenge schema label on format miss, repair attempt, and repair success', async () => {
    // First call: clean exit but only prose — triggers repair.
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: 'no json array here', lastResultText: '' });
    // Second call (repair): valid verdict array.
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: VERDICT_ARRAY });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, ADVERSARIAL_TASK_WITH_HASHES, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // All three telemetry calls must carry the research-spawn-specific schema label,
    // not the release-audit 'audit_challenge' label.
    expectCalledWith(recordFormatMiss, expect.any(String), 'research_spawn_adversarial_challenge');
    expectCalledWith(recordFormatRepairAttempt, 'research_spawn_adversarial_challenge');
    expectCalledWith(recordFormatRepairSuccess, 'research_spawn_adversarial_challenge');
    // Confirm the repair path succeeded and the result was posted.
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
  });

  it('repair re-ask prompt constrains verdict vocabulary to "pass" or "fail"', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: 'no JSON here', lastResultText: '' });
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: VERDICT_ARRAY });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, ADVERSARIAL_TASK_WITH_HASHES, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // The repair re-ask (second call) must explicitly name "pass" and "fail" as
    // the only accepted verdict values so the model emits backend-recognized tokens.
    const reaskPrompt = vi.mocked(runClaudeCode).mock.calls[1][0];
    expect(reaskPrompt).toContain('"pass"');
    expect(reaskPrompt).toContain('"fail"');
  });

  it('repairs adversarial challenge verdicts with backend-recognized pass-fail tokens', async () => {
    // Verifies the full round-trip: malformed first attempt, repaired with "pass"/"fail"
    // vocabulary, and the posted logs parse as the correct recognized verdicts.
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: 'I reviewed the findings but returned no JSON.', lastResultText: '' });
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: VERDICT_ARRAY });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, ADVERSARIAL_TASK_WITH_HASHES, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    const posted = JSON.parse(call.logs as string) as Array<{hash: string; verdict: string; reason: string}>;
    // Every verdict in the posted array must be a backend-recognized token.
    const recognized = new Set(['pass', 'passed', 'approve', 'approved', 'fail', 'failed', 'reject', 'rejected']);
    for (const item of posted) {
      expect(recognized.has(item.verdict.toLowerCase())).toBe(true);
    }
    // Both hashes must be present with appropriate verdicts.
    expect(posted.find(i => i.hash === 'abc123')?.verdict).toBe('pass');
    expect(posted.find(i => i.hash === 'def456')?.verdict).toBe('fail');
  });

  it('repaired array with unrecognized verdicts ("agree"/"disagree") falls through to raw output', async () => {
    const BAD_VERDICT_ARRAY = '[{"hash":"abc123","verdict":"agree","reason":"still holds"},{"hash":"def456","verdict":"disagree","reason":"not present"}]';
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: 'no JSON here', lastResultText: '' });
    // Repair re-ask returns unrecognized vocabulary.
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: BAD_VERDICT_ARRAY });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, ADVERSARIAL_TASK_WITH_HASHES, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    // The repaired array with unrecognized verdicts must NOT be posted as the
    // successful-repair result — the raw repair output is passed through instead.
    expect(call.logs).not.toBe(JSON.stringify(JSON.parse(BAD_VERDICT_ARRAY)));
    // The status must still be COMPLETED (exitCode was 0).
    expect(call.status).toBe('COMPLETED');
  });

  it('adversarial_grader unrecognized verdict on first attempt triggers re-ask (not silent fail)', async () => {
    // First attempt returns parseable JSON but with an unrecognized verdict string.
    // The validateParsedValue hook rejects it, triggering a bounded re-ask.
    // The re-ask here also returns unrecognized vocab, so the raw first output is passed through.
    const UNRECOGNIZED_FIRST = '[{"hash":"abc123","verdict":"agree","reason":"still holds at HEAD"}]';
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: UNRECOGNIZED_FIRST });
    // Re-ask also returns unrecognized vocab → raw passthrough (first run's lastResultText).
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: UNRECOGNIZED_FIRST });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, ADVERSARIAL_TASK_WITH_HASHES, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // Vocab miss now triggers a re-ask — two calls expected.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);

    // The helper emits a schema-validation warning via validateParsedValue.
    const warnCall = vi.mocked(sendLog).mock.calls.find(
      ([, msg]) => typeof msg === 'string' && msg.includes('[WARNING]') && msg.includes('unrecognized'),
    );
    expect(warnCall).toBeDefined();

    // Status is COMPLETED (exitCode was 0) and the raw output is passed through.
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.logs).toContain('agree');
  });

  it('unrecognized verdict re-ask preamble says shape did not match (not missing required keys)', async () => {
    // When validateParsedValue rejects because verdict VALUES are unrecognized
    // (keys 'hash' and 'verdict' ARE present), the re-ask prompt must guide the
    // model to fix the values — not add missing keys that already exist.
    const UNRECOGNIZED_FIRST = '[{"hash":"abc123","verdict":"uncertain","reason":"not sure"}]';
    const RECOGNIZED_RETRY = '[{"hash":"abc123","verdict":"pass","reason":"still holds at HEAD"}]';
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: UNRECOGNIZED_FIRST });
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: RECOGNIZED_RETRY });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, ADVERSARIAL_TASK_WITH_HASHES, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    const reaskPrompt = vi.mocked(runClaudeCode).mock.calls[1][0];
    // The re-ask preamble must NOT claim keys are missing when only values are wrong.
    expect(reaskPrompt).not.toContain('missing required keys');
    // The preamble must tell the agent the field values did not match the schema.
    expect(reaskPrompt).toContain('shape did not match');
    // The schema restatement from the reopenPrompt must still appear.
    expect(reaskPrompt).toContain('MUST be exactly "pass" or "fail"');
  });

  it('first attempt unrecognized vocab triggers re-ask; re-ask with recognized verdicts posts repaired array', async () => {
    const UNRECOGNIZED_FIRST = '[{"hash":"abc123","verdict":"agree","reason":"looks good"},{"hash":"def456","verdict":"disagree","reason":"not present"}]';
    const RECOGNIZED_RETRY = '[{"hash":"abc123","verdict":"pass","reason":"still holds at HEAD"},{"hash":"def456","verdict":"fail","reason":"premise no longer present"}]';
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: UNRECOGNIZED_FIRST });
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: RECOGNIZED_RETRY });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, ADVERSARIAL_TASK_WITH_HASHES, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // Vocab miss on first attempt must trigger exactly one re-ask.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(recordFormatRepairSuccess)).toHaveBeenCalledTimes(1);

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    // The repaired recognized array must be posted, not the unrecognized first attempt.
    const posted = JSON.parse(call.logs as string) as Array<{hash: string; verdict: string}>;
    const recognized = new Set(['pass', 'passed', 'approve', 'approved', 'fail', 'failed', 'reject', 'rejected']);
    for (const item of posted) {
      expect(recognized.has(item.verdict.toLowerCase())).toBe(true);
    }
    expect(call.logs).not.toContain('agree');
    expect(call.logs).not.toContain('disagree');
  });

  it('regression guard: adversarial branch emits a flat array, not an investigation-report object', async () => {
    // The original defect: the handler wrapped adversarial output in the standard
    // investigation-report schema {summary, root_cause, ...}, which has no top-level
    // hash+verdict array for the backend parser to anchor on.  This test asserts the
    // adversarial branch posts the raw challenger output (a flat [{hash, verdict, reason}]
    // array) rather than a JSON.stringify'd investigation-report object.
    const INVESTIGATION_REPORT_SHAPE = JSON.stringify({
      summary: 'Challenger evaluated each finding',
      root_cause: 'See findings array below',
      affected_files: [],
      recommended_fix: 'No action needed',
      severity: 'low',
      confidence: 'high',
      findings: [{ hash: 'abc123', verdict: 'pass', reason: 'holds at HEAD' }],
    });
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: INVESTIGATION_REPORT_SHAPE,
    });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, ADVERSARIAL_TASK_WITH_HASHES, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    // The posted logs must NOT be an investigation-report object — if they were, the
    // backend parser (anchored on a top-level [{hash, verdict}] array) would find no
    // array and fail-close all findings regardless of the actual verdicts.
    const posted = call.logs as string;
    let parsed: unknown;
    try { parsed = JSON.parse(posted); } catch { parsed = null; }
    // Must not be an object with investigation-report keys.
    const isInvestigationReport = (
      parsed !== null &&
      typeof parsed === 'object' &&
      !Array.isArray(parsed) &&
      'summary' in (parsed as Record<string, unknown>)
    );
    expect(isInvestigationReport).toBe(false);
  });
});

// ── handleInvestigate — bounded self-repair on a clean-exit-but-unparseable run ─
describe('handleInvestigate — bounded self-repair on format miss', () => {
  const RECOVERED = {
    summary: 'Token not invalidated on logout',
    root_cause: 'logout handler never calls invalidate()',
    affected_files: [{ path: 'backend/app/auth.py', issue: 'no invalidation' }],
    recommended_fix: 'Call invalidate() in the logout handler',
    severity: 'high',
    confidence: 'high',
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(readHandlerOutput).mockReturnValue(null);
  });

  it('makes exactly one focused re-ask when exitCode=0 but no report parses, and COMPLETES with the recovered report', async () => {
    // First call: clean exit, narrative but no JSON.
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: 'I read auth.py but never emitted the JSON.', lastResultText: '' });
    // Second call (repair): valid investigation report in lastResultText.
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: JSON.stringify(RECOVERED) });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(recordFormatMiss)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairAttempt)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairSuccess)).toHaveBeenCalledTimes(1);

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('INVESTIGATION_REPORT');
    expect(call.logs).toBe(JSON.stringify(RECOVERED));
  });

  it('does NOT record repair success and posts FAILED/parse_failed when the re-ask is also unparseable', async () => {
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: 'still no json', lastResultText: '' });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(recordFormatMiss)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairAttempt)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairSuccess)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('parse_failed');
  });

  it('skips the re-ask entirely (no format telemetry) when the run exited non-zero', async () => {
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: 'crash', lastResultText: '' });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatMiss)).not.toHaveBeenCalled();
    expect(vi.mocked(recordFormatRepairAttempt)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
  });
});

// ── handleMigrate — stale-branch handling and runCodeChangeTask delegation ───
//
// 2026-05-04: handleMigrate's post-Claude commit/rebase/push/PR pipeline was
// replaced with runCodeChangeTask. The handler now: (1) clones, (2) handles
// stale branch deletion (still active production code), (3) builds the
// migration prompt (alembic guidance + ./check test mention +
// CODE_CHANGE_HELPER_GUIDANCE), (4) delegates to runCodeChangeTask. Tests
// assert on the prompt, opts forwarding, and the stale-branch guard.
describe('handleMigrate — stale-branch handling and runCodeChangeTask delegation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Default: ls-remote throws (work branch does not exist on remote)
    vi.mocked(git).mockImplementation((args: string[]) => {
      if (args[0] === 'ls-remote') throw new Error('exit code 2: branch not found');
      if (args[0] === 'rev-parse') return 'abc1234567';
      return '';
    });
    vi.mocked(listOpenPullRequestsForBranch).mockResolvedValue([]);
  });

  it('delegates to runCodeChangeTask with the right opts when prompt is provided', async () => {
    const taskCapability = { agent_mode: 'write', run_shape: 'workflow', output_kind: 'pr' };
    const buildConfig = {
      directories: [{ path: 'backend', type: 'python' }],
    };
    vi.mocked(resolveBuildConfig).mockReturnValueOnce(buildConfig);
    await handleMigrate(
      TASK_ID,
      { ...BASE_PAYLOAD, prompt: 'Add user_role column' },
      { ...BASE_TASK, task_capability: taskCapability }, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    expect(vi.mocked(runCodeChangeTask)).toHaveBeenCalledTimes(1);
    const opts = capturedMigrateOpts();
    expect(opts.taskId).toBe(TASK_ID);
    expect(opts.owner).toBe(OWNER);
    expect(opts.repo).toBe(REPO);
    expect(opts.baseBranch).toBe('main');
    expect(opts.testsOnMainBranch).toBe(true);
    // shortId = taskId sans dashes, first 8 chars → 'taskabc1'
    expect(opts.workBranch).toBe('blueprint/migrate-taskabc1');
    expect(opts.baseCommit).toBe('abc1234567');
    expect(opts.noChangesMessage).toBe('No migration changes generated.');
    expect(opts.buildConfig).toBe(buildConfig);
    expect(opts.taskCapability).toBe(taskCapability);
  });

  it('prepends the acceptance-evidence block when declared', async () => {
    await handleMigrate(
      TASK_ID,
      {
        ...BASE_PAYLOAD,
        prompt: 'Add user_role column',
        acceptance_evidence: {
          kind: 'downstream_artifact',
          artifact_id: 'release-deploy-artifact-registry',
        },
      },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const prompt = capturedMigratePrompt();
    expect(prompt).toContain('## Acceptance Evidence');
    expect(prompt).toContain('release-deploy-artifact-registry');
    expect(prompt).toContain('real, non-empty diff');
  });

  it('includes the shared build-discipline guidance in migration prompts', async () => {
    await handleMigrate(
      TASK_ID,
      { ...BASE_PAYLOAD, prompt: 'Add user_role column' },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const prompt = capturedMigratePrompt();
    expect(prompt).toContain(BUILD_WITH_INTENT_GUIDANCE);
    expect(prompt).toContain(BUILD_ORDER_GUIDANCE);
  });

  it('forwards a custom base_branch and disables tests-on-main accordingly', async () => {
    await handleMigrate(
      TASK_ID,
      { base_branch: 'feature/parent', prompt: 'Add column' },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const opts = capturedMigrateOpts();
    expect(opts.baseBranch).toBe('feature/parent');
    expect(opts.testsOnMainBranch).toBe(false);
  });

  it('falls back to project_metadata.integration_branch when payload.base_branch is absent', async () => {
    await handleMigrate(
      TASK_ID,
      { prompt: 'Add column' },
      { ...BASE_TASK, project_metadata: { integration_branch: 'trunk' } },
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const opts = capturedMigrateOpts();
    expect(opts.baseBranch).toBe('trunk');
    // No promotion_target → testsOnMainBranch falls back to the resolved
    // integration branch ('trunk'), which equals baseBranch → true. The
    // migration must capture the backend baseline on a non-'main' trunk.
    expect(opts.testsOnMainBranch).toBe(true);
  });

  it('treats a non-main integration branch as the trunk for testsOnMainBranch', async () => {
    await handleMigrate(
      TASK_ID,
      { prompt: 'Add column' },
      { ...BASE_TASK, project_metadata: { integration_branch: 'master' } },
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const opts = capturedMigrateOpts();
    expect(opts.baseBranch).toBe('master');
    expect(opts.testsOnMainBranch).toBe(true);
  });

  it('disables tests-on-main when base_branch diverges from a non-main resolved trunk', async () => {
    await handleMigrate(
      TASK_ID,
      { base_branch: 'feature/x', prompt: 'Add column' },
      { ...BASE_TASK, project_metadata: { integration_branch: 'master' } },
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const opts = capturedMigrateOpts();
    expect(opts.baseBranch).toBe('feature/x');
    // base_branch ('feature/x') ≠ resolved trunk ('master') → false.
    expect(opts.testsOnMainBranch).toBe(false);
  });

  it('falls back to main when both payload.base_branch and project_metadata.integration_branch are absent', async () => {
    await handleMigrate(
      TASK_ID,
      { prompt: 'Add column' },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const opts = capturedMigrateOpts();
    expect(opts.baseBranch).toBe('main');
    expect(opts.testsOnMainBranch).toBe(true);
  });

  it('falls back to project_metadata.promotion_target when payload.promotion_target is absent', async () => {
    await handleMigrate(
      TASK_ID,
      { prompt: 'Add column' },
      { ...BASE_TASK, project_metadata: { integration_branch: 'trunk', promotion_target: 'trunk' } },
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const opts = capturedMigrateOpts();
    expect(opts.baseBranch).toBe('trunk');
    // base_branch matches promotion_target → testsOnMainBranch=true
    expect(opts.testsOnMainBranch).toBe(true);
  });

  it('throws when stale remote branch has open PRs (refuses to delete)', async () => {
    // ls-remote succeeds (branch exists); simulate open PR on that branch
    vi.mocked(git).mockImplementation((args: string[]) => {
      if (args[0] === 'ls-remote') return 'abc\trefs/heads/blueprint/migrate-xxx';
      return '';
    });
    vi.mocked(listOpenPullRequestsForBranch).mockResolvedValue([
      { number: 42, html_url: 'https://github.com/owner/repo/pull/42' },
    ]);

    await expect(
      handleMigrate(
        TASK_ID,
        { ...BASE_PAYLOAD, prompt: 'Add column' },
        BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
      ),
    ).rejects.toThrow(/Refusing to delete stale branch.*#42/);

    // Must not have pushed --delete when refusing
    const deleteCall = vi.mocked(git).mock.calls.find(c => {
      const a = c[0];
      return a[0] === 'push' && a.includes('--delete');
    });
    expect(deleteCall).toBeUndefined();
    // Must not have invoked runCodeChangeTask either
    expect(vi.mocked(runCodeChangeTask)).not.toHaveBeenCalled();
  });

  it('deletes stale remote branch and continues when no open PRs exist', async () => {
    // ls-remote succeeds (branch exists); no open PRs
    vi.mocked(git).mockImplementation((args: string[]) => {
      if (args[0] === 'ls-remote') return 'abc\trefs/heads/blueprint/migrate-xxx';
      if (args[0] === 'rev-parse') return 'abc1234567';
      return '';
    });
    vi.mocked(listOpenPullRequestsForBranch).mockResolvedValue([]);

    await handleMigrate(
      TASK_ID,
      { ...BASE_PAYLOAD, prompt: 'Add column' },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const deleteCall = vi.mocked(git).mock.calls.find(c => {
      const a = c[0];
      return a[0] === 'push' && a.includes('--delete');
    });
    expect(deleteCall).toBeDefined();
    // After cleanup, runCodeChangeTask must still be invoked.
    expect(vi.mocked(runCodeChangeTask)).toHaveBeenCalledTimes(1);
  });

  it('migrate prompt instructs agent to bump EXPECTED_SCHEMA_REVISION', async () => {
    await handleMigrate(
      TASK_ID,
      { ...BASE_PAYLOAD, prompt: 'Add column' },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    const prompt = capturedMigratePrompt();
    expect(prompt).toContain('EXPECTED_SCHEMA_REVISION');
    expect(prompt).toContain('app/main.py');
  });

  it('migrate prompt instructs agent to run ./check test before ./pr open (Docker pytest)', async () => {
    // Migrations always touch backend, so tests must run before shipping.
    await handleMigrate(
      TASK_ID,
      { ...BASE_PAYLOAD, prompt: 'Add column' },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    const prompt = capturedMigratePrompt();
    expect(prompt).toContain('./check test');
    expect(prompt).toContain('./pr open');
  });

  it('includes CODE_CHANGE_HELPER_GUIDANCE in the prompt (./check, ./pr, ./done, ./fail)', async () => {
    // The wrapper writes ./check + ./pr + ./done + ./fail; without this
    // guidance Claude has no way to ship its work and the wrapper falls back
    // to FAILED-incomplete on every migration task.
    await handleMigrate(
      TASK_ID,
      { ...BASE_PAYLOAD, prompt: 'Add column' },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    const prompt = capturedMigratePrompt();
    expect(prompt).toContain('./check');
    expect(prompt).toContain('./pr');
    expect(prompt).toContain('./done');
    expect(prompt).toContain('./fail');
  });

  it('includes the migration step list in the prompt (alembic autogenerate, downgrade test, schema revision bump)', async () => {
    await handleMigrate(
      TASK_ID,
      { ...BASE_PAYLOAD, prompt: 'Add column' },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    const prompt = capturedMigratePrompt();
    // Step 1: examine existing migrations
    expect(prompt).toContain('Examine existing migrations');
    // Step 3: run alembic revision --autogenerate
    expect(prompt).toContain('alembic revision --autogenerate');
    // Step 6: alembic upgrade head
    expect(prompt).toContain('alembic upgrade head');
    // Step 7: downgrade round-trip
    expect(prompt).toContain('alembic downgrade');
  });

  it('embeds the user prompt and task title into the migration prompt', async () => {
    await handleMigrate(
      TASK_ID,
      { ...BASE_PAYLOAD, prompt: 'Add user_role column' },
      { ...BASE_TASK, title: 'Migration: user roles' },
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    const prompt = capturedMigratePrompt();
    expect(prompt).toContain('Add user_role column');
    expect(prompt).toContain('Task: Migration: user roles');
  });

  it('injects reserved-revision guidance when the claim carries a reservation', async () => {
    await handleMigrate(
      TASK_ID,
      { ...BASE_PAYLOAD, prompt: 'Add column' },
      {
        ...BASE_TASK,
        migration_reservation: {
          provisional_revision: '0153',
          provisional_down_revision: '0151',
        },
      },
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    const prompt = capturedMigratePrompt();
    expect(prompt).toContain('Reserved migration revision');
    expect(prompt).toContain('revision = "0153"');
    expect(prompt).toContain('actual pre-new-migration chain head');
    expect(prompt).toContain('advisory parent is `0151`; use it only if the actual head cannot be determined');
    expect(prompt).not.toContain('down_revision = "0151"');
    expect(prompt).toContain('0153_<short_description>.py');
    expect(prompt).toContain('EXPECTED_SCHEMA_REVISION = "0153"');
  });

  it('omits reserved-revision guidance when no reservation rides the claim', async () => {
    await handleMigrate(
      TASK_ID,
      { ...BASE_PAYLOAD, prompt: 'Add column' },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    const prompt = capturedMigratePrompt();
    expect(prompt).not.toContain('Reserved migration revision');
  });

  it('throws when no prompt is provided in payload or task', async () => {
    await expect(
      handleMigrate(
        TASK_ID,
        { base_branch: 'main' },
        { title: '', description: '', context: {}, project_context: {} },
        GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
      ),
    ).rejects.toThrow(/No prompt found/);
    // No runCodeChangeTask call when prompt missing
    expect(vi.mocked(runCodeChangeTask)).not.toHaveBeenCalled();
  });

  // Isolate the core migrate step-list (steps 1-8) from the appended generic
  // guidance blocks (CODE_CHANGE_HELPER_GUIDANCE, MIGRATE_TASK_FOCUS, etc.),
  // which are not layout-derived and may mention Blueprint names independently.
  function coreMigrateSteps(prompt: string): string {
    const start = prompt.indexOf('Follow these steps precisely:');
    const end = prompt.indexOf('CRITICAL RULES:');
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    return prompt.slice(start, end);
  }

  it('derives non-Blueprint migration paths from build_config.migration', async () => {
    // A second repo onboards the migrate handler with a different layout.
    vi.mocked(resolveBuildConfig).mockReturnValueOnce({
      directories: [{ path: 'svc', type: 'python' }],
      migration: {
        versions_dir: 'svc/db/migrations',
        models_dir: 'svc/orm',
        alembic_cwd: 'svc',
        schema_revision_file: 'svc/config.py',
        schema_revision_const: 'DB_REVISION',
      },
    });
    await handleMigrate(
      TASK_ID,
      { ...BASE_PAYLOAD, prompt: 'Add column' },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    const steps = coreMigrateSteps(capturedMigratePrompt());
    // Project-specific layout flows through the step list.
    expect(steps).toContain('svc/db/migrations/');
    expect(steps).toContain('svc/orm/');
    expect(steps).toContain('from the svc directory');
    expect(steps).toContain('DB_REVISION');
    expect(steps).toContain('svc/config.py');
    // Blueprint-specific layout strings must NOT appear in the derived steps.
    expect(steps).not.toContain('backend/alembic/versions/');
    expect(steps).not.toContain('backend/app/main.py');
    expect(steps).not.toContain('EXPECTED_SCHEMA_REVISION');
  });

  it('omits the schema-revision step when the project declares no gate (schema_revision_file: "")', async () => {
    vi.mocked(resolveBuildConfig).mockReturnValueOnce({
      directories: [{ path: 'svc', type: 'python' }],
      migration: { schema_revision_file: '' },
    });
    await handleMigrate(
      TASK_ID,
      { ...BASE_PAYLOAD, prompt: 'Add column' },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    const steps = coreMigrateSteps(capturedMigratePrompt());
    // The schema-revision step (step 4) drops out of the core step list.
    expect(steps).not.toContain('EXPECTED_SCHEMA_REVISION');
    expect(steps).not.toMatch(/Bump .* \(MANDATORY\)/);
    // The migration workflow itself still runs.
    expect(steps).toContain('alembic revision --autogenerate');
    // Remaining steps renumber contiguously (no gap at 4): review/apply/
    // downgrade/finish become 4–7 rather than leaving 4 missing.
    expect(steps).toContain('4. **Review the generated migration**');
    expect(steps).toContain('5. **Test the migration applies cleanly**');
    expect(steps).toContain('6. **Test downgrade**');
    expect(steps).toMatch(/7\. Once migration applies cleanly/);
    expect(steps).not.toMatch(/8\. Once migration applies cleanly/);
  });

  it('numbers the schema-revision step 4 and renumbers the rest 5–8 when a gate is declared', async () => {
    vi.mocked(resolveBuildConfig).mockReturnValueOnce({
      directories: [{ path: 'backend', type: 'python' }],
      migration: {},
    });
    await handleMigrate(
      TASK_ID,
      { ...BASE_PAYLOAD, prompt: 'Add column' },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    const steps = coreMigrateSteps(capturedMigratePrompt());
    expect(steps).toMatch(/4\. \*\*Bump EXPECTED_SCHEMA_REVISION \(MANDATORY\)\*\*/);
    expect(steps).toContain('5. **Review the generated migration**');
    expect(steps).toContain('6. **Test the migration applies cleanly**');
    expect(steps).toContain('7. **Test downgrade**');
    expect(steps).toMatch(/8\. Once migration applies cleanly/);
  });
});

// ── resolveMigrationLayout — Blueprint default vs config-driven ──────────────
describe('resolveMigrationLayout', () => {
  it('returns the Blueprint defaults when no build_config is supplied', () => {
    expect(resolveMigrationLayout(undefined)).toEqual({
      versionsDir: 'backend/alembic/versions',
      modelsDir: 'backend/app/models',
      alembicCwd: 'backend',
      schemaRevisionFile: 'backend/app/main.py',
      schemaRevisionConst: 'EXPECTED_SCHEMA_REVISION',
    });
  });

  it('derives defaults from the first configured python directory', () => {
    expect(resolveMigrationLayout({ directories: [{ path: 'svc/', type: 'python' }] })).toEqual({
      versionsDir: 'svc/alembic/versions',
      modelsDir: 'svc/app/models',
      alembicCwd: 'svc',
      schemaRevisionFile: 'svc/app/main.py',
      schemaRevisionConst: 'EXPECTED_SCHEMA_REVISION',
    });
  });

  it('honors explicit migration overrides over the derived defaults', () => {
    expect(resolveMigrationLayout({
      directories: [{ path: 'svc', type: 'python' }],
      migration: {
        versions_dir: 'db/versions',
        schema_revision_const: 'SCHEMA_REV',
      },
    })).toEqual({
      versionsDir: 'db/versions',
      modelsDir: 'svc/app/models',
      alembicCwd: 'svc',
      schemaRevisionFile: 'svc/app/main.py',
      schemaRevisionConst: 'SCHEMA_REV',
    });
  });

  it('treats an empty schema_revision_file as the explicit "no gate" signal', () => {
    expect(resolveMigrationLayout({
      directories: [{ path: 'svc', type: 'python' }],
      migration: { schema_revision_file: '' },
    }).schemaRevisionFile).toBe('');
  });
});

// ── Handler statusRef updates ─────────────────────────────────────────────────
describe('handler statusRef.value updates', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });

  it('handleSecurityAudit sets statusRef before and after runClaudeCode', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue([]);
    const statusRef = STATUS_REF();

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, statusRef);

    // statusRef should have been mutated during the run
    // After runClaudeCode it's still 'Running security audit...'
    expect(statusRef.value).toBe('Running security audit...');
  });

  it('handlePerf sets "Running performance analysis..." status', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue([]);
    const statusRef = STATUS_REF();

    await handlePerf(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, statusRef);

    expect(statusRef.value).toBe('Running performance analysis...');
  });

  it('handleSmoketest sets "Running smoke tests..." status', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue({ passed: true, tests: [] });
    const statusRef = STATUS_REF();

    await handleSmoketest(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, statusRef);

    expect(statusRef.value).toBe('Running smoke tests...');
  });

  it('handleDebug sets "Analyzing failure..." status', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue({ root_cause: '', affected_files: [], fix_prompt: '', confidence: 'low' });
    const statusRef = STATUS_REF();

    await handleDebug(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, statusRef);

    expect(statusRef.value).toBe('Analyzing failure...');
  });

  it('handleInvestigate sets "Running investigation..." status', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue({ summary: '', root_cause: '', affected_files: [], recommended_fix: '', severity: 'low', confidence: 'low' });
    const statusRef = STATUS_REF();

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, statusRef);

    expect(statusRef.value).toBe('Running investigation...');
  });
});

// ── handleMigrate — task-type-specific self-review focus block ────────────────
describe('handleMigrate — migrate self-review focus block', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(git).mockImplementation((args: string[]) => {
      if (args[0] === 'ls-remote') throw new Error('branch not found');
      if (args[0] === 'rev-parse') return 'abc1234567';
      return '';
    });
    vi.mocked(listOpenPullRequestsForBranch).mockResolvedValue([]);
  });

  it('includes MIGRATE_TASK_FOCUS block after CODE_CHANGE_HELPER_GUIDANCE', async () => {
    await handleMigrate(TASK_ID, { ...BASE_PAYLOAD, prompt: 'Add column' }, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedMigratePrompt();
    expect(prompt).toContain('Self-review focus: migrate tasks');
    expect(prompt).toContain('Down-revision chain');
    expect(prompt).toContain('Affected tests');
  });

  it('migrate focus block is positioned after CODE_CHANGE_HELPER_GUIDANCE', async () => {
    await handleMigrate(TASK_ID, { ...BASE_PAYLOAD, prompt: 'Add column' }, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedMigratePrompt();
    const guidanceIdx = prompt.indexOf('Task lifecycle — you own it end-to-end');
    const focusIdx = prompt.indexOf('Self-review focus: migrate tasks');
    expect(guidanceIdx).toBeGreaterThanOrEqual(0);
    expect(focusIdx).toBeGreaterThan(guidanceIdx);
  });

  // Extract just the migrate self-review focus block (bounded at the next
  // "## " heading) so assertions don't pick up unrelated later guidance such
  // as READ_THE_FAILURE_MESSAGE, which also mentions EXPECTED_SCHEMA_REVISION.
  const sliceMigrateFocus = (prompt: string): string => {
    const start = prompt.indexOf('## Self-review focus: migrate tasks');
    const rest = prompt.slice(start + 1);
    const nextHeading = rest.indexOf('\n## ');
    return nextHeading >= 0 ? rest.slice(0, nextHeading) : rest;
  };

  it('focus block emits the project schema-revision file/constant and does not leak Blueprint paths', async () => {
    vi.mocked(resolveBuildConfig).mockReturnValueOnce({
      directories: [{ path: 'svc', type: 'python' }],
      migration: { schema_revision_file: 'svc/app/config.py', schema_revision_const: 'SCHEMA_REV' },
    });
    await handleMigrate(TASK_ID, { ...BASE_PAYLOAD, prompt: 'Add column' }, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const focus = sliceMigrateFocus(capturedMigratePrompt());
    expect(focus).toContain('SCHEMA_REV');
    expect(focus).toContain('svc/app/config.py');
    expect(focus).not.toContain('EXPECTED_SCHEMA_REVISION');
    expect(focus).not.toContain('backend/app/main.py');
  });

  it('focus block omits the schema-revision step when the project declares no gate', async () => {
    vi.mocked(resolveBuildConfig).mockReturnValueOnce({
      directories: [{ path: 'svc', type: 'python' }],
      migration: { schema_revision_file: '' },
    });
    await handleMigrate(TASK_ID, { ...BASE_PAYLOAD, prompt: 'Add column' }, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const focus = sliceMigrateFocus(capturedMigratePrompt());
    expect(focus).not.toContain('EXPECTED_SCHEMA_REVISION');
    expect(focus).not.toContain('backend/app/main.py');
  });

});

// ── Structural prompt invariants (regression-detection) ───────────────────────
// Pins the cross-cutting guidance blocks for every specialized handler.
// A future PR that drops a block from prompt assembly will fail here before merging.
describe('specialized handler structural prompt invariants', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    vi.mocked(git).mockReturnValue('');
    vi.mocked(readHandlerOutput).mockReturnValue(null);
  });

  it('handleMigrate prompt includes all required code-change guidance blocks', async () => {
    vi.mocked(git).mockImplementation((a: string[]) => a[0] === 'rev-parse' ? 'abc' : '');
    await handleMigrate(TASK_ID, { ...BASE_PAYLOAD, prompt: 'add col' }, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const p = capturedMigratePrompt();
    expect(p).toContain('Task lifecycle — you own it end-to-end');        // CODE_CHANGE_HELPER_GUIDANCE
    expect(p).toContain('./playbook set');                                 // PLAYBOOK_GUIDANCE
    expect(p).toContain('Read The Failure Message Literally');             // READ_THE_FAILURE_MESSAGE
    expect(p).toContain('Size Guardrails Are Advisory');                   // SIZE_GUARDRAIL_GUIDANCE
    expect(p).toContain("If You're Retrying");                            // PRIOR_ATTEMPT_DIVERGENCE
    expect(p).toContain('When You Find Bigger Problems Than Your Task');   // BEYOND_SCOPE_REPORTING
    expect(p).toContain('Use Grep/Glob for code search');                 // DEFAULT_TOOL_GUIDANCE
  });

  it('build_config migrate task gets project-aware ./check guidance (no Blueprint stacks)', async () => {
    vi.mocked(git).mockImplementation((a: string[]) => a[0] === 'rev-parse' ? 'abc' : '');
    vi.mocked(resolveBuildConfig).mockReturnValueOnce({
      directories: [
        { path: 'svc', type: 'python', lint_cmd: 'ruff check', build_cmd: 'python -m build', test_cmd: 'pytest -q' },
      ],
    });
    await handleMigrate(TASK_ID, { ...BASE_PAYLOAD, prompt: 'add col' }, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const p = capturedMigratePrompt();
    expect(p).toContain('Task lifecycle — you own it end-to-end');
    expect(p).toContain('./pr open');
    expect(p).toContain('./done');
    expect(p).not.toContain('scripts/test-backend.sh');
    expect(p).not.toContain('backend ruff');
    expect(p).not.toContain('agent tsc');
    expect(p).toContain('python -m build');
  });

  it.each([
    ['handleSecurityAudit', () => handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF())],
    ['handleSmoketest',     () => handleSmoketest(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF())],
    ['handleDebug',         () => handleDebug(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF())],
    ['handlePerf',          () => handlePerf(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF())],
    ['handleInvestigate',   () => handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF())],
    ['handleSelfAudit',     () => handleSelfAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF())],
  ] as [string, () => Promise<void>][])('%s prompt has PLAYBOOK_GUIDANCE + DEFAULT_TOOL_GUIDANCE, no CODE_CHANGE_HELPER_GUIDANCE', async (_name, invoke) => {
    await invoke();
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('./playbook set');                              // PLAYBOOK_GUIDANCE
    expect(p).toContain('Use Grep/Glob for code search');              // DEFAULT_TOOL_GUIDANCE
    expect(p).not.toContain('Task lifecycle — you own it end-to-end'); // no CODE_CHANGE_HELPER_GUIDANCE
  });

  it.each([
    ['handleSecurityAudit', () => handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF()), 'OWASP Top 10'],
    ['handlePerf',          () => handlePerf(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF()), 'N+1 data-access queries'],
    ['handleDebug',         () => handleDebug(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF()), 'Do NOT make any code changes'],
    ['handleInvestigate',   () => handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF()), 'Do NOT modify any files'],
    ['handleSelfAudit',     () => handleSelfAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF()), 'test_coverage'],
    ['handleSmoketest',     () => handleSmoketest(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF()), 'smoke test agent'],
  ] as [string, () => Promise<void>, string][])('%s prompt contains task-type fingerprint phrase', async (_name, invoke, phrase) => {
    await invoke();
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain(phrase);
  });
});

// ── Task-specific content contracts (semantic regression detection) ──────────
// Pins contract elements that define the task scope and LLM behavior.
// A future reword that drops severity enum, component names, or required keys will fail here.
describe('specialized handler task-specific content contracts', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    vi.mocked(git).mockReturnValue('');
    vi.mocked(readHandlerOutput).mockReturnValue(null);
  });

  // ── handleSecurityAudit contract ──────────────────────────────────────────
  describe('handleSecurityAudit', () => {
    it('prompt documents severity enum and output schema keys', async () => {
      await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
      const p = vi.mocked(runClaudeCode).mock.calls[0][0];
      // Severity enum: critical, high, medium, low
      expect(p).toContain('critical');
      expect(p).toContain('high');
      expect(p).toContain('medium');
      expect(p).toContain('low');
      // JSON schema keys
      expect(p).toContain('"title"');
      expect(p).toContain('"severity"');
      expect(p).toContain('"file_path"');
      expect(p).toContain('"fix_prompt"');
      // Evidence-based guidance
      expect(p).toContain('Valid severity values');
    });
  });

  // ── handlePerf contract ──────────────────────────────────────────────────
  describe('handlePerf', () => {
    it('prompt documents evidence-based guidance and area+severity enums', async () => {
      await handlePerf(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
      const p = vi.mocked(runClaudeCode).mock.calls[0][0];
      // Evidence-based guidance
      expect(p).toContain('Only flag issues you can directly observe');
      expect(p).toContain('Evidence-based only');
      // Area enum
      expect(p).toContain('area');
      expect(p).toContain('database');
      expect(p).toContain('api');
      expect(p).toContain('frontend');
      expect(p).toContain('agent');
      // Severity enum
      expect(p).toContain('"severity"');
      expect(p).toContain('high');
      expect(p).toContain('medium');
      expect(p).toContain('low');
      // Valid values documentation
      expect(p).toContain('Valid values:');
    });
  });

  // ── handleDebug contract ─────────────────────────────────────────────────
  describe('handleDebug', () => {
    it('prompt requires all four output keys: root_cause, affected_files, fix_prompt, confidence', async () => {
      await handleDebug(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
      const p = vi.mocked(runClaudeCode).mock.calls[0][0];
      expect(p).toContain('"root_cause"');
      expect(p).toContain('"affected_files"');
      expect(p).toContain('"fix_prompt"');
      expect(p).toContain('"confidence"');
      // Confidence enum values
      expect(p).toContain('high');
      expect(p).toContain('medium');
      expect(p).toContain('low');
    });
  });

  // ── handleInvestigate contract ───────────────────────────────────────────
  describe('handleInvestigate', () => {
    it('prompt documents severity and confidence enums and recommended_fix self-containment', async () => {
      await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
      const p = vi.mocked(runClaudeCode).mock.calls[0][0];
      // Severity enum
      expect(p).toContain('critical');
      expect(p).toContain('high');
      expect(p).toContain('medium');
      expect(p).toContain('low');
      // Confidence enum
      expect(p).toContain('"confidence"');
      expect(p).toContain('high');
      expect(p).toContain('medium');
      expect(p).toContain('low');
      // recommended_fix self-containment rule
      expect(p).toContain('recommended_fix');
      expect(p).toContain('self-contained');
      expect(p).toContain('will NOT see');
      // Read-only constraint
      expect(p).toContain('Do NOT modify any files');
    });

    it('prompt documents real-vs-false regression verdict triage', async () => {
      await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
      const p = vi.mocked(runClaudeCode).mock.calls[0][0];
      // Verdict field with both enum values
      expect(p).toContain('"verdict"');
      expect(p).toContain('real_regression');
      expect(p).toContain('not_a_regression');
      // verdict_reason explanation
      expect(p).toContain('verdict_reason');
      // Common false-positive triggers explicitly enumerated
      expect(p).toContain('intentional');
      expect(p).toContain('known-incomplete pipeline step');
      // Don't fabricate fixes when not_a_regression
      expect(p).toContain('do NOT fabricate a fix');
    });

    it('renders the ABSENT discount note + stale refs when citation_state is absent', async () => {
      const absentTask = {
        ...BASE_TASK,
        context: {
          _provenance_warnings: {
            citation_state: 'absent',
            broken_refs: ['backend/app/gone.py'],
            ungrounded_refs: ['backend/app/x.py:vanished_fn'],
          },
        },
      };
      await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, absentTask, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
      const p = vi.mocked(runClaudeCode).mock.calls[0][0];
      expect(p).toContain('Citation-provenance notice');
      expect(p).toContain('ABSENT on the current');
      expect(p).toContain('discount its actionability');
      // The specific stale refs reach the agent.
      expect(p).toContain('backend/app/gone.py');
      expect(p).toContain('backend/app/x.py:vanished_fn');
      // The absent note must NOT tell the agent the defect remains actionable.
      expect(p).not.toContain('NOT presumed resolved');
    });

    it('renders the PARTIAL note + stale refs when grounded but some refs are broken', async () => {
      const partialTask = {
        ...BASE_TASK,
        context: {
          _provenance_warnings: {
            citation_state: 'present',
            broken_refs: ['backend/app/moved.py'],
            ungrounded_refs: ['backend/app/y.py:renamed_fn'],
          },
        },
      };
      await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, partialTask, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
      const p = vi.mocked(runClaudeCode).mock.calls[0][0];
      expect(p).toContain('Citation-provenance notice');
      expect(p).toContain('PARTIALLY grounded');
      expect(p).toContain('NOT presumed resolved');
      expect(p).toContain('remains actionable');
      // The specific stale refs reach the agent.
      expect(p).toContain('backend/app/moved.py');
      expect(p).toContain('backend/app/y.py:renamed_fn');
      // The partial note must NOT use the stronger absent discount framing.
      expect(p).not.toContain('discount its actionability');
    });

    it('omits the provenance notice entirely when no _provenance_warnings on context', async () => {
      await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
      const p = vi.mocked(runClaudeCode).mock.calls[0][0];
      expect(p).not.toContain('Citation-provenance notice');
    });
  });

  // ── handleRepoBootstrap contract ────────────────────────────────────────
  describe('handleRepoBootstrap', () => {
    it('accepts a repo overview with all seven fields', () => {
      const repoOverview = {
        tech_stack: 'TypeScript, FastAPI, Postgres',
        directory_layout: 'agent/ for the runner, backend/ for the API',
        first_steps_summary: 'Install deps, run the build, then run tests',
        entry_points: 'agent/src/index.ts and backend/app/main.py',
        test_layout: 'backend/testing for pytest; agent/src/*.test.ts for vitest',
        key_conventions: 'Conventional commits and feature branches',
        environment_requirements: 'Node 22, Python 3.12, DATABASE_URL in .env',
        source_revision: 'abc1234def5678',
      };

      expect(isRepoBootstrapOverview(repoOverview)).toBe(true);
      if (!isRepoBootstrapOverview(repoOverview)) throw new Error('expected repo overview');
      expect(repoOverview.entry_points).toBe('agent/src/index.ts and backend/app/main.py');
      expect(repoOverview.test_layout).toBe('backend/testing for pytest; agent/src/*.test.ts for vitest');
      expect(repoOverview.key_conventions).toBe('Conventional commits and feature branches');
      expect(repoOverview.environment_requirements).toBe('Node 22, Python 3.12, DATABASE_URL in .env');
      expect(repoOverview.source_revision).toBe('abc1234def5678');
    });

    it('persists a valid repo overview unchanged', async () => {
      const repoOverview = {
        tech_stack: 'TypeScript, FastAPI, Postgres',
        directory_layout: 'agent/ for the runner, backend/ for the API',
        first_steps_summary: 'Install deps, run the build, then run tests',
        entry_points: 'agent/src/index.ts and backend/app/main.py',
        test_layout: 'backend/testing for pytest; agent/src/*.test.ts for vitest',
        key_conventions: 'Conventional commits and feature branches',
        environment_requirements: 'Node 22, Python 3.12, DATABASE_URL in .env',
        source_revision: 'abc1234def5678',
      };
      vi.mocked(readHandlerOutput).mockReturnValue(repoOverview);
      vi.mocked(runClaudeCode).mockResolvedValue({
        exitCode: 0,
        allOutput: 'bootstrap transcript tail',
        lastResultText: '',
      });

      await handleRepoBootstrap(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

      const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
      expect(call.status).toBe('COMPLETED');
      expect(call.repo_overview).toEqual(repoOverview);
      expect(call.logs).toBe(JSON.stringify(repoOverview));
    });

    it('repo_bootstrap prompt requests all repo overview fields including source revision', async () => {
      vi.mocked(readHandlerOutput).mockReturnValue(null);
      await handleRepoBootstrap(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
      const p = vi.mocked(runClaudeCode).mock.calls[0][0];
      expect(p).toContain('"tech_stack"');
      expect(p).toContain('"directory_layout"');
      expect(p).toContain('"first_steps_summary"');
      expect(p).toContain('"entry_points"');
      expect(p).toContain('if discoverable: main executable(s) or server entry files');
      expect(p).toContain('"test_layout"');
      expect(p).toContain('if discoverable: test directory structure and how to run subsets');
      expect(p).toContain('"key_conventions"');
      expect(p).toContain('if discoverable: branch naming, commit style, PR conventions');
      expect(p).toContain('"environment_requirements"');
      expect(p).toContain('if discoverable: required env vars, runtime versions, .env.example notes');
      expect(p).toContain('"source_revision"');
      expect(p).toContain('checked-out HEAD SHA after clone/checkout');
    });

    it('repo_bootstrap prompt includes playbook seeding guidance', async () => {
      vi.mocked(readHandlerOutput).mockReturnValue(null);
      await handleRepoBootstrap(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
      const p = vi.mocked(runClaudeCode).mock.calls[0][0];
      expect(p).toContain('./playbook set');    // PLAYBOOK_GUIDANCE present
      expect(p).toContain('environment setup'); // bootstrap-specific save note
    });

    it.each([
      ['partial object', { tech_stack: 'TypeScript', directory_layout: 'agent/' }],
      ['array output', ['TypeScript', 'agent/', 'tests']],
    ] as const)('drops invalid bootstrap overview %s', async (_label, invalidOverview) => {
      vi.mocked(readHandlerOutput).mockReturnValue(invalidOverview);
      vi.mocked(runClaudeCode).mockResolvedValue({
        exitCode: 0,
        allOutput: 'bootstrap transcript tail',
        lastResultText: '',
      });

      await handleRepoBootstrap(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

      const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
      expect(call.status).toBe('COMPLETED');
      expect(call.repo_overview).toBeUndefined();
      expect(call.logs).toBe('bootstrap transcript tail');
    });

    it('isCommandMemoryOutcome accepts the three statuses and rejects others', () => {
      expect(isCommandMemoryOutcome({ status: 'success' })).toBe(true);
      expect(isCommandMemoryOutcome({ status: 'missing_commands', detail: 'none found' })).toBe(true);
      expect(isCommandMemoryOutcome({ status: 'writeback_failed', updated_paths: ['.'] })).toBe(true);
      // Reject unknown status, non-objects, arrays, and overview-shaped payloads.
      expect(isCommandMemoryOutcome({ status: 'confirmed' })).toBe(false);
      expect(isCommandMemoryOutcome({ tech_stack: 'x' })).toBe(false);
      expect(isCommandMemoryOutcome(['success'])).toBe(false);
      expect(isCommandMemoryOutcome(null)).toBe(false);
    });

    it('reports command_memory from the command-memory file independently of the overview', async () => {
      const repoOverview = {
        tech_stack: 'TypeScript',
        directory_layout: 'agent/',
        first_steps_summary: 'install, build, test',
      };
      // Distinct files: overview vs. command memory. Reported independently so a
      // good overview never implies confirmed commands.
      vi.mocked(readHandlerOutput).mockImplementation((_taskId, outputType) =>
        outputType === 'bootstrap_overview'
          ? repoOverview
          : { status: 'missing_commands', detail: 'no runnable test command' },
      );
      vi.mocked(runClaudeCode).mockResolvedValue({
        exitCode: 0,
        allOutput: 'bootstrap transcript tail',
        lastResultText: '',
      });

      await handleRepoBootstrap(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

      const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
      expect(call.repo_overview).toEqual(repoOverview);
      expect(call.command_memory).toEqual({ status: 'missing_commands', detail: 'no runnable test command' });
    });

    it('clamps oversized command_memory detail and updated_paths before posting status', async () => {
      const repoOverview = {
        tech_stack: 'TypeScript',
        directory_layout: 'agent/',
        first_steps_summary: 'install, build, test',
      };
      const detail = 'x'.repeat(2001);
      const updatedPaths = Array.from({ length: 55 }, (_, index) => `dir-${index}`);
      vi.mocked(readHandlerOutput).mockImplementation((_taskId, outputType) =>
        outputType === 'bootstrap_overview'
          ? repoOverview
          : { status: 'success', detail, updated_paths: updatedPaths },
      );
      vi.mocked(runClaudeCode).mockResolvedValue({
        exitCode: 0,
        allOutput: 'bootstrap transcript tail',
        lastResultText: '',
      });

      await handleRepoBootstrap(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

      const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
      expect(call.command_memory).toEqual({
        status: 'success',
        detail: 'x'.repeat(2000),
        updated_paths: updatedPaths.slice(0, 50),
      });
    });

    it('omits command_memory when the command-memory file is absent/invalid', async () => {
      vi.mocked(readHandlerOutput).mockImplementation((_taskId, outputType) =>
        outputType === 'bootstrap_overview'
          ? { tech_stack: 'TypeScript', directory_layout: 'agent/', first_steps_summary: 'go' }
          : null,
      );
      vi.mocked(runClaudeCode).mockResolvedValue({
        exitCode: 0,
        allOutput: 'bootstrap transcript tail',
        lastResultText: '',
      });

      await handleRepoBootstrap(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

      const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
      expect(call.command_memory).toBeUndefined();
    });

    it('repo_bootstrap prompt requests the command-memory outcome file', async () => {
      vi.mocked(readHandlerOutput).mockReturnValue(null);
      await handleRepoBootstrap(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
      const p = vi.mocked(runClaudeCode).mock.calls[0][0];
      expect(p).toContain(`/tmp/command_memory_${TASK_ID}.json`);
      expect(p).toContain('success | missing_commands | writeback_failed');
    });

    // ── Cache-optimized prompt prefix (v24 cost lever, direction d-27685510) ──
    // The repo-bootstrap prompt must lead with stable literal/config guidance so
    // the automatically-cacheable prefix (Anthropic prefix-cache reads bill at
    // ~10% of base input) is maximized. Two builds of an identical payload but
    // different wall-clock / git HEAD must produce a BYTE-IDENTICAL stable prefix;
    // task-variable content (contextSection, retryContext) lives in a contiguous
    // trailing tail. Mirrors standard.test.ts's byte-identical-prefix pattern.
    describe('assembleRepoBootstrapPrompt', () => {
      const STABLE = {
        bootstrapCoreBody: 'CORE_BOOTSTRAP_INSTRUCTIONS',
        fetchGuidance: '## MID-RUN FETCH\nfetch-verbs\n',
        claudeMdSection: '\n\n## Project Instructions (CLAUDE.md)\nBE CONSISTENT',
        playbookNote: '\nNote: save setup knowledge\n',
      } as const;

      it('yields an identical stable prefix when only task-variable content changes (wall-clock/HEAD proxy)', () => {
        const build1 = assembleRepoBootstrapPrompt({
          ...STABLE,
          contextSection: 'CTX_MARKER failed X (2 days ago)\n',
          retryContext: '',
        });
        const build2 = assembleRepoBootstrapPrompt({
          ...STABLE,
          contextSection: 'CTX_MARKER failed X (9 days ago)\n',
          retryContext: 'RETRY_DELTA HEAD def5678',
        });
        expect(build1.stablePrefix).toBe(build2.stablePrefix);
        expect(build1.stablePrefix.length).toBeGreaterThan(0);
        expect(build1.variableBody).not.toBe(build2.variableBody);
      });

      it('places every stable guidance constant strictly before any task-variable section', () => {
        const { fullPrompt } = assembleRepoBootstrapPrompt({
          ...STABLE,
          contextSection: 'UNIQUE_CONTEXT_MARKER\n',
          retryContext: 'RETRY_MARKER',
        });
        const firstVariableIdx = Math.min(
          ...['UNIQUE_CONTEXT_MARKER', 'RETRY_MARKER']
            .map((m) => fullPrompt.indexOf(m))
            .filter((i) => i >= 0),
        );
        for (const stable of [
          STABLE.bootstrapCoreBody,
          DEFAULT_TOOL_GUIDANCE,
          STABLE.fetchGuidance,
          STABLE.claudeMdSection,
          PLAYBOOK_GUIDANCE,
          STABLE.playbookNote,
        ]) {
          const idx = fullPrompt.indexOf(stable);
          expect(idx).toBeGreaterThanOrEqual(0);
          expect(idx).toBeLessThan(firstVariableIdx);
        }
      });

      it('preserves every sent section (byte-order change only, nothing dropped)', () => {
        const { fullPrompt } = assembleRepoBootstrapPrompt({
          ...STABLE,
          contextSection: 'CTX',
          retryContext: 'RETRY',
        });
        for (const piece of [
          STABLE.bootstrapCoreBody, STABLE.fetchGuidance, STABLE.claudeMdSection,
          STABLE.playbookNote, 'CTX', 'RETRY',
          DEFAULT_TOOL_GUIDANCE, PLAYBOOK_GUIDANCE,
        ]) {
          expect(fullPrompt).toContain(piece);
        }
      });
    });

    it('handleRepoBootstrap leads with stable guidance and is byte-identical across wall-clock/HEAD', async () => {
      vi.mocked(readHandlerOutput).mockReturnValue(null);
      try {
        // Build 1 — contextPrefix carries a wall-clock/HEAD-derived tail.
        vi.mocked(buildContextPrefix).mockReturnValue('CTX_TAIL_MARKER failed X (2 days ago) HEAD aaaaaaaa');
        await handleRepoBootstrap(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
        const prompt1 = vi.mocked(runClaudeCode).mock.calls[0][0];

        // Build 2 — same payload, different wall-clock/HEAD-derived contextPrefix.
        vi.mocked(runClaudeCode).mockClear();
        vi.mocked(buildContextPrefix).mockReturnValue('CTX_TAIL_MARKER failed X (30 days ago) HEAD bbbbbbbb');
        await handleRepoBootstrap(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
        const prompt2 = vi.mocked(runClaudeCode).mock.calls[0][0];

        // Stable guidance precedes the task-variable context marker...
        const markerIdx1 = prompt1.indexOf('CTX_TAIL_MARKER');
        expect(markerIdx1).toBeGreaterThan(0);
        expect(prompt1.indexOf(DEFAULT_TOOL_GUIDANCE)).toBeGreaterThanOrEqual(0);
        expect(prompt1.indexOf(DEFAULT_TOOL_GUIDANCE)).toBeLessThan(markerIdx1);
        expect(prompt1.indexOf(PLAYBOOK_GUIDANCE)).toBeLessThan(markerIdx1);

        // ...and the leading prefix is byte-identical across the two builds.
        const prefix1 = prompt1.slice(0, markerIdx1);
        const prefix2 = prompt2.slice(0, prompt2.indexOf('CTX_TAIL_MARKER'));
        expect(prefix1.length).toBeGreaterThan(0);
        expect(prefix1).toBe(prefix2);
      } finally {
        vi.mocked(buildContextPrefix).mockReturnValue('');
      }
    });
  });

  // ── handleSelfAudit contract ────────────────────────────────────────────
  describe('handleSelfAudit', () => {
    it('prompt documents four component scores and recommended_mode enum', async () => {
      await handleSelfAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
      const p = vi.mocked(runClaudeCode).mock.calls[0][0];
      // Four component scores
      expect(p).toContain('test_coverage');
      expect(p).toContain('friction_density');
      expect(p).toContain('claim_consistency');
      expect(p).toContain('failure_mix');
      // recommended_mode enum
      expect(p).toContain('recommended_mode');
      expect(p).toContain('audit');
      expect(p).toContain('fundamentals');
      expect(p).toContain('ship');
      expect(p).toContain('pause');
      // Score range
      expect(p).toContain('[0, 100]');
    });
  });

  // ── handleSmoketest contract ────────────────────────────────────────────
  describe('handleSmoketest', () => {
    it('prompt requests suites structure and guardrail_violations array', async () => {
      await handleSmoketest(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
      const p = vi.mocked(runClaudeCode).mock.calls[0][0];
      // Suite structure
      expect(p).toContain('suites.startup');
      expect(p).toContain('suites.curated');
      expect(p).toContain('suites.regression');
      // Result structure elements
      expect(p).toContain('startup');
      expect(p).toContain('curated');
      expect(p).toContain('regression');
      expect(p).toContain('guardrail_violations');
      // Run order
      expect(p).toContain('IN ORDER');
    });
  });
});

// ── PARALLEL_AUDIT_GUIDANCE injection ────────────────────────────────────────
describe('Parallel audit guidance injection', () => {
  const PARALLEL_TASK = { ...BASE_TASK, project_metadata: { enable_parallel_review: true } };
  const SERIAL_TASK = { ...BASE_TASK, project_metadata: { enable_parallel_review: false } };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    vi.mocked(readHandlerOutput).mockReturnValue(null);
  });

  it('handleSecurityAudit injects PARALLEL_AUDIT_GUIDANCE when flag is on', async () => {
    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, PARALLEL_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain(PARALLEL_AUDIT_GUIDANCE);
  });

  it('handleSecurityAudit omits PARALLEL_AUDIT_GUIDANCE when flag is off', async () => {
    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, SERIAL_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).not.toContain(PARALLEL_AUDIT_GUIDANCE);
  });

  it('handleInvestigate injects PARALLEL_AUDIT_GUIDANCE when flag is on', async () => {
    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, PARALLEL_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain(PARALLEL_AUDIT_GUIDANCE);
  });

  it('handleInvestigate omits PARALLEL_AUDIT_GUIDANCE when flag is off', async () => {
    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, SERIAL_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).not.toContain(PARALLEL_AUDIT_GUIDANCE);
  });

  it('handleDebug injects PARALLEL_AUDIT_GUIDANCE when flag is on', async () => {
    const debugPayload = { ...BASE_PAYLOAD, error_logs: 'some error', failed_task_title: 'bad task' };
    await handleDebug(TASK_ID, debugPayload, PARALLEL_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain(PARALLEL_AUDIT_GUIDANCE);
  });

  it('handleDebug omits PARALLEL_AUDIT_GUIDANCE when flag is off', async () => {
    const debugPayload = { ...BASE_PAYLOAD, error_logs: 'some error', failed_task_title: 'bad task' };
    await handleDebug(TASK_ID, debugPayload, SERIAL_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).not.toContain(PARALLEL_AUDIT_GUIDANCE);
  });

  it('handlePerf injects PARALLEL_AUDIT_GUIDANCE when flag is on', async () => {
    await handlePerf(TASK_ID, BASE_PAYLOAD, PARALLEL_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain(PARALLEL_AUDIT_GUIDANCE);
  });

  it('handleSelfAudit injects PARALLEL_AUDIT_GUIDANCE when flag is on', async () => {
    await handleSelfAudit(TASK_ID, BASE_PAYLOAD, PARALLEL_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain(PARALLEL_AUDIT_GUIDANCE);
  });
});

// ── Per-project integration_branch cascade (resolveBaseBranch) ───────────────
// Each analysis-style handler must clone the per-project integration branch
// when payload.base_branch is absent. Without the cascade, projects whose
// integration branch is not 'main' (e.g. 'master', 'production') would
// silently clone the wrong branch.
describe('resolveBaseBranch cascade — analysis handlers honor project_metadata.integration_branch', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });

  const TASK_WITH_INTEGRATION = {
    ...BASE_TASK,
    project_metadata: { integration_branch: 'master' },
  };
  const PAYLOAD_NO_BASE_BRANCH = {}; // no base_branch

  it('handleSecurityAudit clones project_metadata.integration_branch when payload.base_branch is absent', async () => {
    await handleSecurityAudit(
      TASK_ID, PAYLOAD_NO_BASE_BRANCH, TASK_WITH_INTEGRATION,
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    const opts = vi.mocked(prepareWorkspaceForRole).mock.calls[0][0];
    expect(opts.branch).toBe('master');
    expect(opts.baseBranch).toBe('master');
  });

  it('handlePerf clones project_metadata.integration_branch when payload.base_branch is absent', async () => {
    await handlePerf(
      TASK_ID, PAYLOAD_NO_BASE_BRANCH, TASK_WITH_INTEGRATION,
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    const opts = vi.mocked(prepareWorkspaceForRole).mock.calls[0][0];
    expect(opts.branch).toBe('master');
    expect(opts.baseBranch).toBe('master');
  });

  it('handleInvestigate clones project_metadata.integration_branch when payload.base_branch is absent', async () => {
    await handleInvestigate(
      TASK_ID, { prompt: 'investigate' }, TASK_WITH_INTEGRATION,
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    const opts = vi.mocked(prepareWorkspaceForRole).mock.calls[0][0];
    expect(opts.branch).toBe('master');
    expect(opts.baseBranch).toBe('master');
  });

  it('handleSelfAudit clones project_metadata.integration_branch when payload.base_branch is absent', async () => {
    await handleSelfAudit(
      TASK_ID, PAYLOAD_NO_BASE_BRANCH, TASK_WITH_INTEGRATION,
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    const opts = vi.mocked(prepareWorkspaceForRole).mock.calls[0][0];
    expect(opts.branch).toBe('master');
    expect(opts.baseBranch).toBe('master');
  });

  it('falls back to main when both payload.base_branch and project_metadata.integration_branch are absent', async () => {
    await handleSecurityAudit(
      TASK_ID, PAYLOAD_NO_BASE_BRANCH, BASE_TASK,
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    const opts = vi.mocked(prepareWorkspaceForRole).mock.calls[0][0];
    expect(opts.branch).toBe('main');
    expect(opts.baseBranch).toBe('main');
  });

  it('payload.base_branch overrides project_metadata.integration_branch when both are present', async () => {
    await handleSecurityAudit(
      TASK_ID, { base_branch: 'feature/x' }, TASK_WITH_INTEGRATION,
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    const opts = vi.mocked(prepareWorkspaceForRole).mock.calls[0][0];
    expect(opts.branch).toBe('feature/x');
    expect(opts.baseBranch).toBe('feature/x');
  });
});

// ── READ_ONLY_BASH_WRITE_NOTE — prompt inclusion guards ──────────────────────
// Each read-only specialized handler must instruct the agent to use Bash to
// write its /tmp output file because Write/Edit tools are disabled. These tests
// assert the shared note appears in the main prompt, preventing silent
// regression when prompts are refactored.
describe('read-only specialized handlers — Bash write note present in prompt', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    vi.mocked(readHandlerOutput).mockReturnValue(null);
  });

  it('handleSmoketest prompt contains READ_ONLY_BASH_WRITE_NOTE', async () => {
    await handleSmoketest(TASK_ID, { ...BASE_PAYLOAD, suites: {} }, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain(READ_ONLY_BASH_WRITE_NOTE);
  });

  it('handleDebug prompt contains READ_ONLY_BASH_WRITE_NOTE', async () => {
    const debugPayload = { ...BASE_PAYLOAD, prompt: 'investigate the crash', error_logs: 'error: crash' };
    await handleDebug(TASK_ID, debugPayload, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain(READ_ONLY_BASH_WRITE_NOTE);
  });

  it('handlePerf prompt contains READ_ONLY_BASH_WRITE_NOTE', async () => {
    await handlePerf(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain(READ_ONLY_BASH_WRITE_NOTE);
  });

  it('handleInvestigate prompt contains READ_ONLY_BASH_WRITE_NOTE', async () => {
    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain(READ_ONLY_BASH_WRITE_NOTE);
  });

  it('handleRepoBootstrap prompt contains READ_ONLY_BASH_WRITE_NOTE', async () => {
    vi.mocked(readHandlerOutput)
      .mockReturnValueOnce({ tech_stack: 'node', directory_layout: 'src/', first_steps_summary: 'npm install', entry_points: '', test_layout: '', key_conventions: '', environment_requirements: '', source_revision: 'abc' })
      .mockReturnValueOnce({ status: 'success', detail: '', updated_paths: [] })
      .mockReturnValueOnce({ purpose: 'test app', key_concepts: {}, critical_user_flows: [], architectural_gotchas: [], source: 'README.md' });
    await handleRepoBootstrap(TASK_ID, { ...BASE_PAYLOAD, repo_url: 'https://github.com/example/repo' }, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain(READ_ONLY_BASH_WRITE_NOTE);
  });
});
