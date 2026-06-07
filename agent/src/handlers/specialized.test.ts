import { describe, it, expect, vi, beforeEach } from 'vitest';

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
    gitCommit: vi.fn().mockReturnValue(true),
    setupWorkspace: vi.fn(),
    runClaudeCode: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' }),
    cloneWithRetry: vi.fn().mockResolvedValue(undefined),
    sendHeartbeatSync: vi.fn(),
    listOpenPullRequestsForBranch: vi.fn().mockResolvedValue([]),
  };
});

vi.mock('../workspacePrep.js', () => ({
  prepareWorkspaceForRole: vi.fn().mockResolvedValue(undefined),
}));

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

vi.mock('../build-config.js', () => ({
  resolveBuildConfig: vi.fn().mockReturnValue(undefined),
}));

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
vi.mock('./code-change.js', () => ({
  runCodeChangeTask: vi.fn().mockResolvedValue(undefined),
}));

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
  handleMigrate,
  handleSelfAudit,
  resolveMigrationLayout,
} from './specialized.js';
import {
  persistAndPostStatus, readHandlerOutput, runClaudeCode,
  git, listOpenPullRequestsForBranch,
} from '../index.js';
import { resolveBuildConfig } from '../build-config.js';
import { runCodeChangeTask } from './code-change.js';
import { recordFormatRepairAttempt, recordFormatRepairSuccess } from '../telemetry.js';
import { execFileSync } from 'child_process';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import { PARALLEL_AUDIT_GUIDANCE } from '../prompt.js';

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

  it('reports FAILED when smoketestResult.passed is false', async () => {
    const smoketestResult = { passed: false, tests: [{ name: 'pytest', status: 'fail', message: 'AssertionError' }] };
    vi.mocked(readHandlerOutput).mockReturnValue(smoketestResult);

    await handleSmoketest(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('SMOKETEST_RESULT');
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
    expect(vi.mocked(runClaudeCode).mock.calls[1][0]).toContain('Return ONLY the JSON object');
    expect(recordFormatRepairAttempt).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairSuccess).toHaveBeenCalledTimes(1);
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
    await handleMigrate(
      TASK_ID,
      { ...BASE_PAYLOAD, prompt: 'Add user_role column' },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
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
    expect(prompt).toContain('down_revision = "0151"');
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
    } as ReturnType<typeof resolveBuildConfig>);
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
    } as ReturnType<typeof resolveBuildConfig>);
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
    } as ReturnType<typeof resolveBuildConfig>);
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
    ['handlePerf',          () => handlePerf(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF()), 'N+1 database queries'],
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

// ── handleSmoketest — qualifier-smoketest candidate-SHA pinning (C4) ──────────
//
// The qualifier-smoketest must run against ONE pinned candidate commit, not
// whatever `automain` has drifted to by the time the agent VM starts. The
// backend passes the snapshotted `candidate_sha` in the task payload. Since
// `git clone --branch <X>` cannot accept a SHA, the handler clones the branch
// normally then `git fetch origin <sha>` + `git checkout --detach <sha>`.
describe('handleSmoketest — candidate-SHA pinning', () => {
  const CANDIDATE_SHA = 'b2c3d4e5f60718293a4b5c6d7e8f901234567890';
  const QUALIFIER_PAYLOAD = { base_branch: 'automain', candidate_sha: CANDIDATE_SHA };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    vi.mocked(git).mockReturnValue('');
    vi.mocked(execFileSync).mockReturnValue(Buffer.from(''));
    vi.mocked(readHandlerOutput).mockReturnValue({ passed: true, tests: [] });
  });

  it('clones with FULL history (depth 0) when a candidate_sha is pinned', async () => {
    await handleSmoketest(TASK_ID, QUALIFIER_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const opts = vi.mocked(prepareWorkspaceForRole).mock.calls[0][0];
    expect(opts.branch).toBe('automain');
    // depth 0 → full clone; the snapshotted commit may predate a shallow depth.
    expect(opts.depth).toBe(0);
  });

  it('fetches and detaches onto the EXACT candidate commit', async () => {
    await handleSmoketest(TASK_ID, QUALIFIER_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const gitExec = vi.mocked(execFileSync).mock.calls.filter(c => c[0] === 'git');
    const fetchCall = gitExec.find(c => (c[1] as string[])[0] === 'fetch');
    const checkoutCall = gitExec.find(c => (c[1] as string[])[0] === 'checkout');
    expect(fetchCall?.[1]).toEqual(['fetch', 'origin', CANDIDATE_SHA]);
    // Detached checkout pins the working tree onto the exact commit.
    expect(checkoutCall?.[1]).toEqual(['checkout', '--detach', CANDIDATE_SHA]);
    // The smoketest still runs after pinning.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
  });

  it('reads candidate_sha from task.context as a fallback', async () => {
    const taskWithCtx = { ...BASE_TASK, context: { candidate_sha: CANDIDATE_SHA } };
    await handleSmoketest(
      TASK_ID, { base_branch: 'automain' }, taskWithCtx, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const checkoutCall = vi.mocked(execFileSync).mock.calls.find(
      c => c[0] === 'git' && (c[1] as string[])[0] === 'checkout',
    );
    expect(checkoutCall?.[1]).toEqual(['checkout', '--detach', CANDIDATE_SHA]);
  });

  it('does NOT fetch/checkout a SHA for a plain post-merge smoketest (no candidate_sha)', async () => {
    // BASE_PAYLOAD carries no candidate_sha — the plain post-merge smoketest
    // path must be untouched: a shallow clone and no SHA pinning.
    await handleSmoketest(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const opts = vi.mocked(prepareWorkspaceForRole).mock.calls[0][0];
    expect(opts.depth).toBe(100); // unchanged shallow clone
    const checkoutCall = vi.mocked(execFileSync).mock.calls.find(
      c => c[0] === 'git' && (c[1] as string[])[0] === 'checkout',
    );
    expect(checkoutCall).toBeUndefined();
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
  });

  it('FAILS LOUD when the candidate-SHA fetch fails — does not run the smoketest', async () => {
    vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
      const a = args as string[];
      if (Array.isArray(a) && a[0] === 'fetch') throw new Error('fatal: remote unreachable');
      return Buffer.from('');
    });

    await handleSmoketest(TASK_ID, QUALIFIER_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // A smoketest of the wrong commit is meaningless — the handler must abort.
    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
    expect(call.failure_category).toBe('clone_failed');
    expect(call.logs).toContain(CANDIDATE_SHA);
  });

  it('FAILS LOUD when the candidate-SHA checkout fails', async () => {
    vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
      const a = args as string[];
      if (Array.isArray(a) && a[0] === 'checkout') throw new Error('fatal: reference is not a tree');
      return Buffer.from('');
    });

    await handleSmoketest(TASK_ID, QUALIFIER_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('clone_failed');
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
