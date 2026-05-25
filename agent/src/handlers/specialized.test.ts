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
  handleReleaseAudit,
  resolveCandidateSha,
} from './specialized.js';
import {
  persistAndPostStatus, readHandlerOutput, runClaudeCode,
  git, listOpenPullRequestsForBranch,
} from '../index.js';
import { runCodeChangeTask } from './code-change.js';
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

  it('still includes CODE_CHANGE_HELPER_GUIDANCE alongside the focus block', async () => {
    await handleMigrate(TASK_ID, { ...BASE_PAYLOAD, prompt: 'Add column' }, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = capturedMigratePrompt();
    expect(prompt).toContain('Task lifecycle — you own it end-to-end');
    expect(prompt).toContain('Self-review focus: migrate tasks');
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

// ── handleReleaseAudit — pinned-candidate clone + diff-range pre-flight ───────
//
// C2/C4: a release audit must score ONE PINNED candidate commit. The bare
// branch name `automain` does NOT resolve as a revision after cloning `main`
// (`git fetch origin automain` only updates FETCH_HEAD, never a local ref), so
// a `main...automain` range can never resolve and the audit would ALWAYS fail.
// The fix: the backend passes a full 40-char `candidate_sha` in the task
// payload; the handler (1) clones `main` with FULL history (depth 0), (2)
// fetches the EXACT candidate commit (`git fetch origin <sha>`), (3) pre-flight
// `git rev-parse --verify <sha>^{commit}` + `git rev-list --count
// main...<sha>` and FAILS LOUD if the range does not resolve.
describe('handleReleaseAudit — pinned candidate + diff-range pre-flight', () => {
  // A full 40-char hex SHA (the backend pins automain HEAD this way).
  const CANDIDATE_SHA = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678';
  const DIFF_RANGE = `main...${CANDIDATE_SHA}`;
  const RELEASE_PAYLOAD = { base_branch: 'main', candidate_sha: CANDIDATE_SHA };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    // git() mock returns '' (success) by default — rev-parse + rev-list pass.
    vi.mocked(git).mockReturnValue('');
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    // execFileSync (child_process) — git fetch succeeds by default.
    vi.mocked(execFileSync).mockReturnValue(Buffer.from(''));
  });

  it('clones main with FULL history (depth 0), not a shallow DEFAULT_CLONE_DEPTH clone', async () => {
    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(prepareWorkspaceForRole)).toHaveBeenCalledTimes(1);
    const opts = vi.mocked(prepareWorkspaceForRole).mock.calls[0][0];
    expect(opts.branch).toBe('main');
    expect(opts.baseBranch).toBe('main');
    // depth 0 → full clone (no --depth flag). A shallow clone would leave the
    // merge-base unreachable for a long-diverged candidate.
    expect(opts.depth).toBe(0);
  });

  it('fetches the EXACT candidate SHA — not the bare `automain` branch name', async () => {
    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const fetchCalls = vi.mocked(execFileSync).mock.calls.filter(
      c => c[0] === 'git' && Array.isArray(c[1]) && (c[1] as string[])[0] === 'fetch',
    );
    expect(fetchCalls.length).toBeGreaterThan(0);
    const fetchArgs = fetchCalls[0][1] as string[];
    // `git fetch origin <candidate_sha>` — a SHA fetch leaves a real commit
    // object behind, which `main...<sha>` can resolve. A bare-branch fetch
    // (`git fetch origin automain`) only updates FETCH_HEAD and is the bug.
    expect(fetchArgs).toEqual(['fetch', 'origin', CANDIDATE_SHA]);
    expect(fetchArgs).not.toContain('automain');
  });

  it('reads candidate_sha from task.context as a fallback when absent from the payload', async () => {
    // The backend may stamp candidate_sha under task.context; the handler must
    // still resolve it (the contract is "from the payload", context is a
    // defensive fallback so the handler keeps working either way).
    const payloadNoSha = { base_branch: 'main' };
    const taskWithCtx = { ...BASE_TASK, context: { candidate_sha: CANDIDATE_SHA } };

    await handleReleaseAudit(TASK_ID, payloadNoSha, taskWithCtx, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const fetchCalls = vi.mocked(execFileSync).mock.calls.filter(
      c => c[0] === 'git' && Array.isArray(c[1]) && (c[1] as string[])[0] === 'fetch',
    );
    expect((fetchCalls[0][1] as string[])).toEqual(['fetch', 'origin', CANDIDATE_SHA]);
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
  });

  it('runs the agent when the diff range resolves (rev-parse + rev-list succeed)', async () => {
    vi.mocked(git).mockImplementation((args: string[]) => {
      if (args[0] === 'rev-list') return '137'; // range resolves: 137 commits
      return '';
    });

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // Pre-flight verified the candidate SHA is a reachable commit object.
    const revParseCall = vi.mocked(git).mock.calls.find(c => c[0][0] === 'rev-parse');
    expect(revParseCall).toBeDefined();
    expect(revParseCall?.[0]).toContain(`${CANDIDATE_SHA}^{commit}`);
    // Pre-flight ran `git rev-list --count main...<candidate_sha>`.
    const revListCall = vi.mocked(git).mock.calls.find(c => c[0][0] === 'rev-list');
    expect(revListCall).toBeDefined();
    expect(revListCall?.[0]).toContain('--count');
    expect(revListCall?.[0]).toContain(DIFF_RANGE);
    // Agent runs and a RELEASE_AUDIT_REPORT is emitted.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
  });

  it('interpolates main...<candidate_sha> into the agent prompt — never the bare automain name', async () => {
    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    // The diff commands the agent is told to run must use the pinned SHA.
    expect(prompt).toContain(`git diff ${DIFF_RANGE}`);
    expect(prompt).toContain(`git diff --stat ${DIFF_RANGE}`);
    expect(prompt).toContain(CANDIDATE_SHA);
    // The prompt must NOT instruct the agent to diff the bare branch name.
    expect(prompt).not.toContain('git diff main...automain');
  });

  it('FAILS LOUD with diff_range_unresolvable when no valid candidate_sha is supplied', async () => {
    // Missing candidate_sha is a real contract bug — there is no commit to
    // diff. The handler must not fall back to the bare `automain` name.
    await handleReleaseAudit(TASK_ID, { base_branch: 'main' }, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(prepareWorkspaceForRole)).not.toHaveBeenCalled();
    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
    expect(call.failure_category).toBe('diff_range_unresolvable');
    expect(call.logs).toContain('candidate_sha');
  });

  it('FAILS LOUD with diff_range_unresolvable when candidate_sha is malformed (not 40-char hex)', async () => {
    // A truncated SHA, branch name, or garbage value must be rejected — only a
    // full 40-char hex SHA is a valid pinned candidate.
    await handleReleaseAudit(
      TASK_ID, { base_branch: 'main', candidate_sha: 'automain' }, BASE_TASK,
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('diff_range_unresolvable');
    // The rejected value is surfaced for debuggability.
    expect(call.logs).toContain('automain');
  });

  it('FAILS LOUD with diff_range_unresolvable when git rev-list does not resolve the range', async () => {
    // Merge-base unreachable → `git rev-list --count main...<sha>` errors.
    vi.mocked(git).mockImplementation((args: string[]) => {
      if (args[0] === 'rev-list') {
        throw new Error(`fatal: Invalid symmetric difference expression ${DIFF_RANGE}`);
      }
      return '';
    });

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // The agent must NOT run — it could misread a diff error as "empty diff"
    // and emit a false audit_score:1.0.
    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
    expect(call.failure_category).toBe('diff_range_unresolvable');
    expect(call.logs).toContain(DIFF_RANGE);
    expect(call.logs).toContain('merge-base');
  });

  it('FAILS LOUD with diff_range_unresolvable when rev-parse cannot verify the candidate commit', async () => {
    // The SHA fetch reported success but the object is not actually a reachable
    // commit → `git rev-parse --verify <sha>^{commit}` errors. Fail loud.
    vi.mocked(git).mockImplementation((args: string[]) => {
      if (args[0] === 'rev-parse') {
        throw new Error('fatal: Needed a single revision');
      }
      return '';
    });

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('diff_range_unresolvable');
  });

  it('FAILS LOUD when the candidate-SHA fetch fails', async () => {
    // The candidate commit cannot be fetched → range cannot resolve. The
    // handler must fail loud, not run the agent.
    vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
      const a = args as string[];
      if (Array.isArray(a) && a[0] === 'fetch') {
        throw new Error('fatal: could not read from remote repository');
      }
      return Buffer.from('');
    });

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('diff_range_unresolvable');
  });

  it('does not run the rev-list pre-flight at all when the candidate fetch failed', async () => {
    vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
      const a = args as string[];
      if (Array.isArray(a) && a[0] === 'fetch') throw new Error('network down');
      return Buffer.from('');
    });

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // No rev-list call — the fetch already failed so there is nothing to check.
    const revListCall = vi.mocked(git).mock.calls.find(c => c[0][0] === 'rev-list');
    expect(revListCall).toBeUndefined();
  });

  it('emits RELEASE_AUDIT_REPORT preserving the audit_score/test_erosion artifact contract on success', async () => {
    // The qualifier's _score_audit reads audit_score + test_erosion.{deleted_test_files,
    // assertion_delta, test_count_delta}. The handler must pass that shape through.
    const report = {
      audit_score: 0.82,
      test_erosion: { deleted_test_files: [], assertion_delta: 9, test_count_delta: 2 },
      findings: [{ severity: 'medium', summary: 'x', file: 'a.py' }],
    };
    vi.mocked(readHandlerOutput).mockReturnValue(report);

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('RELEASE_AUDIT_REPORT');
    const stored = JSON.parse(call.logs as string) as typeof report;
    expect(stored.audit_score).toBe(0.82);
    expect(stored.test_erosion.deleted_test_files).toEqual([]);
    expect(stored.test_erosion.assertion_delta).toBe(9);
    expect(stored.test_erosion.test_count_delta).toBe(2);
  });
});

// ── resolveCandidateSha — payload/context SHA resolution + validation ─────────
describe('resolveCandidateSha', () => {
  const FULL_SHA = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678';

  it('reads a full 40-char SHA from the payload', () => {
    expect(resolveCandidateSha({ candidate_sha: FULL_SHA }, {})).toBe(FULL_SHA);
  });

  it('falls back to task.context.candidate_sha when the payload lacks it', () => {
    expect(resolveCandidateSha({}, { context: { candidate_sha: FULL_SHA } })).toBe(FULL_SHA);
  });

  it('prefers the payload SHA over the context SHA', () => {
    const other = 'ffffffffffffffffffffffffffffffffffffffff';
    expect(
      resolveCandidateSha({ candidate_sha: FULL_SHA }, { context: { candidate_sha: other } }),
    ).toBe(FULL_SHA);
  });

  it('lowercases and trims an uppercase / padded SHA', () => {
    expect(resolveCandidateSha({ candidate_sha: `  ${FULL_SHA.toUpperCase()}  ` }, {})).toBe(FULL_SHA);
  });

  it('returns null for a short SHA, a branch name, or garbage', () => {
    expect(resolveCandidateSha({ candidate_sha: 'a1b2c3d' }, {})).toBeNull();
    expect(resolveCandidateSha({ candidate_sha: 'automain' }, {})).toBeNull();
    expect(resolveCandidateSha({ candidate_sha: 'z'.repeat(40) }, {})).toBeNull(); // non-hex
    expect(resolveCandidateSha({ candidate_sha: '' }, {})).toBeNull();
  });

  it('returns null when neither payload nor context carries a SHA', () => {
    expect(resolveCandidateSha({}, {})).toBeNull();
    expect(resolveCandidateSha({}, { context: {} })).toBeNull();
    expect(resolveCandidateSha({ candidate_sha: 123 }, { context: 'not-an-object' })).toBeNull();
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
