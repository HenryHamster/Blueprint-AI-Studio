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
    buildSelfReviewPrompt: vi.fn().mockReturnValue(''),
  };
});

vi.mock('../build-config.js', () => ({
  resolveBuildConfig: vi.fn().mockReturnValue(undefined),
}));

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
import {
  handleSecurityAudit,
  handleSmoketest,
  handleDebug,
  handlePerf,
  handleInvestigate,
  handleMigrate,
  handleSelfAudit,
} from './specialized.js';
import {
  persistAndPostStatus, readHandlerOutput, runClaudeCode,
  git, listOpenPullRequestsForBranch,
} from '../index.js';
import { runCodeChangeTask } from './code-change.js';
import { execFileSync } from 'child_process';

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

// ── handleSecurityAudit — JSON extraction fallback ────────────────────────────
describe('handleSecurityAudit — JSON extraction fallback', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });

  it('uses readHandlerOutput result directly when it returns findings', async () => {
    const findings = [{ title: 'SQL Injection', severity: 'critical' }];
    vi.mocked(readHandlerOutput).mockReturnValue(findings);

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.logs).toBe(JSON.stringify(findings));
    expect(call.artifact_type).toBe('SECURITY_REPORT');
  });

  it('falls back to lastResultText when it contains "severity" and readHandlerOutput returns null', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    const lastResultText = '[{"title":"XSS","severity":"high","fix_prompt":"..."}]';
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText });

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.logs).toBe(lastResultText);
  });

  it('extracts JSON from allOutput via regex when lastResultText lacks "severity"', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    const jsonBlock = '[{"title":"Path Traversal","severity":"medium"}]';
    const allOutput = `Some preamble text\n${jsonBlock}\nSome trailing text`;
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput, lastResultText: 'no match here' });

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.logs).toBe(jsonBlock);
  });

  it('falls back to lastResultText when neither file nor regex match is found', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: 'no json here',
      lastResultText: 'plain text result',
    });

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.logs).toBe('plain text result');
  });

  it('falls back to truncated allOutput when lastResultText is empty and no regex match', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: 'no json content',
      lastResultText: '',
    });

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.logs).toBe('no json content');
  });

  it('sets status COMPLETED and artifact_type SECURITY_REPORT on exitCode 0', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue([]);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('SECURITY_REPORT');
  });

  it('sets status FAILED and artifact_type ERROR_LOG on non-zero exitCode', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: 'err', lastResultText: '' });

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
  });
});

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
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: malformedJson,
    });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('parse_failed');
  });

  it('falls through to parse_failed when allOutput has malformed JSON with required keys', async () => {
    const malformedJson = 'Investigation complete.\n{"root_cause": undefined, "recommended_fix": "fix it"}\nDone.';
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: malformedJson,
      lastResultText: '',
    });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('parse_failed');
  });

  it('sets status FAILED and artifact_type ERROR_LOG on non-zero exitCode with result', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue({ summary: 'partial', root_cause: '', affected_files: [], recommended_fix: '', severity: 'low', confidence: 'low' });
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: '', lastResultText: '' });

    await handleInvestigate(TASK_ID, INVESTIGATE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
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

// ── handleSelfAudit — findings parsing and artifact emission ─────────────────
describe('handleSelfAudit — findings ingestion', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });

  it('persists findings and emits SELF_AUDIT_REPORT when readHandlerOutput returns a structured object', async () => {
    const findings = {
      findings: [{
        system_id: 'sys-auth',
        score: 72,
        components: { test_coverage: 80, friction_density: 65, claim_consistency: 75, failure_mix: 70 },
        top_issues: ['Missing integration tests'],
        recommended_mode: 'fundamentals',
        evidence: [{ file: 'backend/app/auth.py', summary: 'no tests' }],
      }],
    };
    vi.mocked(readHandlerOutput).mockReturnValue(findings);

    await handleSelfAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('SELF_AUDIT_REPORT');
    expect(call.logs).toBe(JSON.stringify(findings));
  });

  it('extracts JSON object from allOutput when readHandlerOutput returns null', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    const jsonBlock = '{"findings":[{"system_id":"sys-x","score":50,"recommended_mode":"audit"}]}';
    const allOutput = `preamble\n${jsonBlock}\ntrailer`;
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput, lastResultText: 'no signal' });

    await handleSelfAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    // We re-stringify after parsing, so just verify the resulting JSON has the same shape
    const stored = JSON.parse(call.logs as string) as { findings: Array<{ system_id: string }> };
    expect(stored.findings).toHaveLength(1);
    expect(stored.findings[0].system_id).toBe('sys-x');
    expect(call.artifact_type).toBe('SELF_AUDIT_REPORT');
  });

  it('falls back to wrapping a bare JSON array in {findings: [...]}', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    const arrBlock = '[{"system_id":"sys-y","score":80,"recommended_mode":"ship"}]';
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0, allOutput: `noise ${arrBlock} more noise`, lastResultText: '',
    });

    await handleSelfAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    const stored = JSON.parse(call.logs as string) as { findings: Array<{ system_id: string }> };
    expect(stored.findings).toHaveLength(1);
    expect(stored.findings[0].system_id).toBe('sys-y');
  });

  it('sets status FAILED and ERROR_LOG on non-zero exitCode', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: 'crash', lastResultText: '' });

    await handleSelfAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
    expect(call.failure_category).toBe('claude_code_failed');
  });

  it('embeds focus_systems list into the prompt when provided', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue({ findings: [] });

    await handleSelfAudit(
      TASK_ID,
      { ...BASE_PAYLOAD, focus_systems: ['sys-auth', 'sys-billing'], max_systems: 4 },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const promptArg = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(promptArg).toContain('sys-auth');
    expect(promptArg).toContain('sys-billing');
    expect(promptArg).toContain('Audit ONLY these systems');
  });
});
